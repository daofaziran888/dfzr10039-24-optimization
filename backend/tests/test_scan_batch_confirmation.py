"""增量扫描批次确认：重启续跑去重、确认优先级、交付证据门禁与消费幂等。

场景与题述一一对应：
- 站点 A 的 B1 报 S4 新增、S5 变更，工作台刷新后再跑同一输入，消费端每项只收一次；
- B2 写差异后中断（只认领不确认），接班重试时加入 B3 与另一站点的 C1，
  重新核对确认优先级、消费顺序与输入快照，C1 的确认位置单独可查。
"""
from __future__ import annotations

from app.services.scan_batch import (
    BATCH_CONFIRMED, BATCH_CONSUMED, BATCH_PENDING, CONSUME_MODULE,
    SCAN_BATCH_MODULE, SCAN_CHANGE_MODULE, ScanBatchService,
)
from app.store import store


def _fresh_ledger() -> ScanBatchService:
    """每个用例开头清空台账并返回新服务，模拟服务冷启动且无历史数据。"""
    for module in (SCAN_BATCH_MODULE, SCAN_CHANGE_MODULE, CONSUME_MODULE):
        store.rows(module).clear()
    # 清掉测试站点在增量标记/差异台账里的残留读位，保证用例互不串状态
    for module in ("incr_crawl", "diff_check"):
        store.rows(module)[:] = [
            row for row in store.rows(module)
            if str(row.get("目标站点")) not in ("A", "C", "D")
        ]
    return ScanBatchService()


def _restart_ledger() -> ScanBatchService:
    """模拟工位刷新/进程重启：只重建服务实例，批次台账原样保留可续跑。"""
    return ScanBatchService()


def _pages(*pairs: tuple[str, dict]) -> list[dict]:
    return [{"page_key": key, "fields": dict(fields)} for key, fields in pairs]


# ---------------------------------------------------------------------------
# B1：同一输入重复认领去重，消费端每项只收到一次
# ---------------------------------------------------------------------------

def test_b1_same_input_after_refresh_keeps_single_batch_and_single_delivery() -> None:
    ledger = _fresh_ledger()
    # 基线含 S5 旧值，B1 中 S5 才构成「修改」；S4 不在基线中构成「新增」
    baseline = _pages(
        ("S1", {"title": "旧"}),
        ("S2", {"title": "保持"}),
        ("S5", {"title": "S5旧内容"}),
    )
    first_input = _pages(
        ("S1", {"title": "旧"}),
        ("S2", {"title": "保持"}),
        ("S4", {"title": "S4新页面"}),
        ("S5", {"title": "S5已变更"}),
    )
    # 先认领一个基线批次并确认，作为站点 A 的已确认读位
    base = ledger.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=baseline, source_job="JOB-A0",
        baseline_pages=[])
    ledger.confirm_batch(base.batch_id)
    ledger.consume_batch(base.batch_id, consumer="dw")

    before_change_rows = len(store.rows(SCAN_CHANGE_MODULE))

    # B1 第一次扫描：S4 新增、S5 变更（显式给出对照基线）
    b1 = ledger.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=first_input, source_job="JOB-A1",
        baseline_pages=baseline)
    assert b1.added_count == 1 and b1.updated_count == 1 and b1.removed_count == 0
    change_ids_first = sorted(c.change_id for c in b1.changes)
    pages_first = {c.page_key: c.change_type for c in b1.changes}
    assert pages_first == {"S4": "新增", "S5": "修改"}

    # 工位刷新（服务重建）后再跑同一输入：必须认领回同一个批次
    reloaded = _restart_ledger()
    b1_again = reloaded.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=first_input, source_job="JOB-A1",
        baseline_pages=baseline)
    assert b1_again.batch_id == b1.batch_id, "重复认领只保留一份有效批次"
    assert b1_again.claim_count == 2, "重复认领应累加认领次数以便追查"
    assert sorted(c.change_id for c in b1_again.changes) == change_ids_first, \
        "已经发出的差异保留原编号并继续可读"
    assert len(store.rows(SCAN_CHANGE_MODULE)) == before_change_rows + 2, \
        "重复认领不能产生第二份差异"
    assert b1_again.status == BATCH_PENDING, "中断未确认的批次刷新后仍是待确认"

    reloaded.confirm_batch(b1.batch_id)
    record1 = reloaded.consume_batch(b1.batch_id, consumer="dw")
    assert record1["新增数量"] == 1 and record1["修改数量"] == 1

    # 再跑同一输入并再次消费：消费端两项各只能收到一次
    b1_third = reloaded.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=first_input, source_job="JOB-A1",
        baseline_pages=baseline)
    assert b1_third.batch_id == b1.batch_id
    record2 = reloaded.consume_batch(b1.batch_id, consumer="dw")
    assert record2["消费编号"] == record1["消费编号"], "同一批次重复消费只保留一份记录"
    consumptions = [
        row for row in reloaded.consumption_records("A")
        if str(row.get("批次编号")) == b1.batch_id
    ]
    assert len(consumptions) == 1
    assert consumptions[0]["扫描快照"] == b1.snapshot_hash, "消费记录必须留扫描快照"
    delivered = [r for r in store.rows(SCAN_CHANGE_MODULE)
                 if r["批次编号"] == b1.batch_id]
    assert len(delivered) == 2 and all(r["status"] == "已交付" for r in delivered)


# ---------------------------------------------------------------------------
# B2 中断续跑 + B3 同站竞争 + C1 独立站点
# ---------------------------------------------------------------------------

def test_b2_interrupted_resume_with_b3_and_c1_priority_order() -> None:
    ledger = _fresh_ledger()
    baseline = _pages(("S1", {"v": "1"}))
    base = ledger.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=baseline, source_job="JOB-A0",
        baseline_pages=[])
    ledger.confirm_batch(base.batch_id)
    ledger.consume_batch(base.batch_id)

    # B2：留下差异以后还没有确认就中断（不调用 confirm）
    b2_input = _pages(("S1", {"v": "1"}), ("S2", {"v": "新"}))
    b2 = ledger.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=b2_input, source_job="JOB-B2",
        baseline_pages=baseline)
    assert b2.status == BATCH_PENDING

    # 接班时进程重启，B2 恢复作业与 B3 新扫描同时提交，另加独立站点 C 的 C1
    reloaded = _restart_ledger()
    b2_resume = reloaded.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=b2_input, source_job="JOB-B2",
        baseline_pages=baseline)
    assert b2_resume.batch_id == b2.batch_id, "B2 中断后续跑必须认领回原批次"
    assert b2_resume.claim_count == 2

    b3_input = _pages(("S1", {"v": "1"}), ("S2", {"v": "新"}), ("S3", {"v": "B3"}))
    b3 = reloaded.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=b3_input, source_job="JOB-B3")
    assert b3.batch_id != b2.batch_id

    c1_input = _pages(("C-S1", {"v": "C"}))
    c1 = reloaded.claim_scan(
        target_site="C", cursor_id="INCR-C", pages=c1_input, source_job="JOB-C1")

    # 同站点确认优先级：B3 不能越过 B2 先确认
    try:
        reloaded.confirm_batch(b3.batch_id)
    except RuntimeError as exc:
        assert "更早的待确认批次" in str(exc)
    else:  # pragma: no cover - 防御性断言
        raise AssertionError("B3 不应早于 B2 确认")

    # 独立站点 C1 不受站点 A 排队影响，确认位置单独可查
    c1_confirmed = reloaded.confirm_batch(c1.batch_id)
    assert c1_confirmed.confirm_seq == 1, "C1 是站点 C 的第一批，确认序号为 1"
    position_c = reloaded.site_position("C")
    assert position_c["目标站点"] == "C"
    assert position_c["最近批次"] == c1.batch_id
    assert position_c["待确认批次"] == 0

    # B2 先确认（站点 A 上排在基线批次之后），再轮到 B3，序号严格递增
    b2_confirmed = reloaded.confirm_batch(b2.batch_id)
    b3_confirmed = reloaded.confirm_batch(b3.batch_id)
    assert b2_confirmed.confirm_seq is not None and b3_confirmed.confirm_seq is not None
    assert b2_confirmed.confirm_seq < b3_confirmed.confirm_seq
    assert b3_confirmed.confirm_seq == b2_confirmed.confirm_seq + 1

    # 消费顺序：全局下一批按确认序号，C1（序号1、站点C）与 B2（序号1、站点A）
    # 按台账 id 先后；站点 A 维度下下一批必须是 B2
    next_for_a = reloaded.next_deliverable("A")
    assert next_for_a is not None and next_for_a.batch_id == b2.batch_id

    # 交付证据门禁：B2 没有实际交付前，B3 不能被消费
    try:
        reloaded.consume_batch(b3.batch_id)
    except RuntimeError as exc:
        assert "实际交付证据" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("不能跳过没有交付证据的前一批")

    reloaded.consume_batch(b2.batch_id, consumer="dw")
    next_for_a = reloaded.next_deliverable("A")
    assert next_for_a is not None and next_for_a.batch_id == b3.batch_id
    reloaded.consume_batch(b3.batch_id, consumer="dw")

    # 独立站点读位互不影响：站点 A 的推进没有改动站点 C
    position_a = reloaded.site_position("A")
    position_c_after = reloaded.site_position("C")
    # 站点 A：基线批次 + B2 + B3 三批均已消费
    assert position_a["已消费批次"] == 3
    assert position_c_after["读位游标"] == position_c["读位游标"]
    assert reloaded.site_position("C")["已消费批次"] == 0

    # 输入快照可追查：恢复的 B2 留的就是中断前那份输入
    resumed = reloaded.get_batch(b2.batch_id)
    assert resumed is not None
    assert resumed.input_snapshot == b2_input
    assert resumed.snapshot_hash == b2.snapshot_hash


# ---------------------------------------------------------------------------
# 差异检测清单从同一份批次确认记录取数
# ---------------------------------------------------------------------------

def test_diff_checklist_and_cursor_advance_share_one_confirmation() -> None:
    ledger = _fresh_ledger()
    baseline = _pages(("S1", {"title": "旧"}))
    base = ledger.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=baseline, source_job="JOB-A0",
        baseline_pages=[])
    ledger.confirm_batch(base.batch_id)
    ledger.consume_batch(base.batch_id)

    current = _pages(("S1", {"title": "新"}), ("S9", {"title": "新增页"}))
    batch = ledger.claim_scan(
        target_site="A", cursor_id="INCR-A", pages=current, source_job="JOB-AX",
        end_cursor="cursor-002", baseline_pages=baseline)
    # 确认前差异检测清单（diff_check 台账）不应出现该批次结论
    assert all(
        str(row.get("批次编号")) != batch.batch_id
        for row in store.rows("diff_check")
    )
    ledger.confirm_batch(batch.batch_id)

    checklist = ledger.changes_for_diff_checklist("A")
    linked = [c for c in checklist if c.batch_id == batch.batch_id]
    assert {c.page_key for c in linked} == {"S1", "S9"}
    assert {c.change_type for c in linked} == {"修改", "新增"}
    # 差异检测台账（diff_check）与批次确认记录共用编号与快照
    diff_rows = [
        row for row in store.rows("diff_check")
        if row.get("批次编号") == batch.batch_id
    ]
    assert len(diff_rows) == 2
    assert all(row["扫描快照"] == batch.snapshot_hash for row in diff_rows)

    # 游标只在确认时落库推进，读位停在本批续扫游标
    position = ledger.site_position("A")
    assert position["读位游标"] == "cursor-002"
    incr_rows = [r for r in store.rows("incr_crawl") if r.get("目标站点") == "A"]
    assert incr_rows and incr_rows[-1]["当前游标"] == "cursor-002"


def test_delete_change_is_delivered_with_evidence() -> None:
    ledger = _fresh_ledger()
    baseline = _pages(("S1", {"v": "1"}), ("S2", {"v": "2"}))
    base = ledger.claim_scan(
        target_site="D", cursor_id="INCR-D", pages=baseline, source_job="JOB-D0",
        baseline_pages=[])
    ledger.confirm_batch(base.batch_id)
    ledger.consume_batch(base.batch_id)

    current = _pages(("S1", {"v": "1"}))  # S2 被删除
    batch = ledger.claim_scan(
        target_site="D", cursor_id="INCR-D", pages=current, source_job="JOB-D1")
    assert batch.removed_count == 1
    ledger.confirm_batch(batch.batch_id)
    record = ledger.consume_batch(batch.batch_id, consumer="mq")
    assert record["删除数量"] == 1 and record["目标站点"] == "D"
    assert ledger.get_batch(batch.batch_id).status == BATCH_CONSUMED
