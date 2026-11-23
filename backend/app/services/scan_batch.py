"""增量扫描批次确认台账：写差异、确认进度、断点续跑、下游消费共用同一份记录。

设计要点（对应批次确认改造需求）：

1. 扫描批次先把差异写入台账，续扫游标只在批次「确认」时落库推进，
   工位刷新/进程重启后从台账恢复，不再依赖进程内变量。
2. 同一站点同一份输入快照重复认领只保留一个批次（快照哈希幂等），
   已发出的差异保留原编号、持续可读，认领次数累加可追查。
3. 同一站点多份候选输入按先到先得的确认序号排队（FIFO），
   前一批没有实际交付证据（消费记录）时，后一批不能越过它被消费，
   且一个站点的读位推进不会改动其它站点的读位。
4. 批次确认结论同步到差异检测清单，差异检测从同一份批次确认记录取数；
   下游只消费「本次获准读取」的新增/修改/删除，每次消费都登记
   站点、批次与扫描快照，重复消费只保留一份有效变动。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.models import (
    SCAN_CHANGE_ADDED, SCAN_CHANGE_REMOVED, SCAN_CHANGE_UPDATED,
    ScanBatch, ScanChange,
)
from app.services.base import BaseModuleService
from app.store import store

SCAN_BATCH_MODULE = "scan_batch"
SCAN_CHANGE_MODULE = "scan_change"
CONSUME_MODULE = "batch_consume"
INCR_MODULE = "incr_crawl"
DIFF_MODULE = "diff_check"

# 批次与差异的状态流转。
BATCH_PENDING = "待确认"
BATCH_CONFIRMED = "已确认"
BATCH_CONSUMED = "已消费"

CHANGE_PENDING = "待确认"
CHANGE_CONFIRMED = "已确认"
CHANGE_DELIVERED = "已交付"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_snapshot_hash(target_site: str, cursor_id: str,
                          pages: list[dict[str, Any]],
                          baseline_pages: list[dict[str, Any]] | None = None) -> str:
    """扫描快照哈希：站点+游标+基线+输入四要素一致才是同一次扫描。

    基线纳入哈希后，「基线批次的输入恰好与本批输入相同」不会被误判为
    重复认领；而同一次扫描在重启后用相同基线与输入续跑时仍命中同一批次。
    """
    payload = json.dumps(
        {
            "site": target_site,
            "cursor": cursor_id,
            "baseline": baseline_pages or [],
            "pages": pages,
        },
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ScanBatchService(BaseModuleService):
    MODULE = SCAN_BATCH_MODULE
    ENTITY = "扫描批次"
    OPERATION_TERM = "扫描"
    LIST_FIELDS = [
        "批次编号", "目标站点", "游标标记", "来源作业", "起始游标", "续扫游标",
        "扫描快照", "认领次数", "新增数量", "修改数量", "删除数量",
        "确认序号", "确认时间", "交付时间", "检测时间",
    ]
    REQUIRED_FIELDS = ["批次编号", "目标站点", "游标标记"]
    STATUS_ORDER = [BATCH_PENDING, BATCH_CONFIRMED, BATCH_CONSUMED]
    ACTION_RULES = {
        "确认批次": BATCH_CONFIRMED,
        "登记交付": BATCH_CONSUMED,
    }
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 台账读取辅助：全部落在共享 store，进程重启后数据仍在
    # ------------------------------------------------------------------
    def _batch_rows(self) -> list[dict[str, Any]]:
        return store.rows(SCAN_BATCH_MODULE)

    def _change_rows(self) -> list[dict[str, Any]]:
        return store.rows(SCAN_CHANGE_MODULE)

    def _consume_rows(self) -> list[dict[str, Any]]:
        return store.rows(CONSUME_MODULE)

    def _find_batch_row(self, batch_id: str) -> dict[str, Any] | None:
        for row in self._batch_rows():
            if str(row.get("批次编号")) == batch_id:
                return row
        return None

    def _changes_of(self, batch_id: str) -> list[ScanChange]:
        return [
            ScanChange.from_row(row) for row in self._change_rows()
            if str(row.get("批次编号")) == batch_id
        ]

    def get_batch(self, batch_id: str) -> ScanBatch | None:
        row = self._find_batch_row(batch_id)
        if row is None:
            return None
        return ScanBatch.from_row(row, self._changes_of(batch_id))

    def list_batches(self, target_site: str | None = None) -> list[ScanBatch]:
        rows = self._batch_rows()
        if target_site:
            rows = [row for row in rows if str(row.get("目标站点")) == target_site]
        return [
            ScanBatch.from_row(row, self._changes_of(str(row.get("批次编号"))))
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 阶段 1：认领扫描（写差异 + 快照留痕），重复认领幂等
    # ------------------------------------------------------------------
    def claim_scan(
        self,
        *,
        target_site: str,
        cursor_id: str,
        pages: list[dict[str, Any]],
        source_job: str = "",
        end_cursor: str = "",
        baseline_pages: list[dict[str, Any]] | None = None,
    ) -> ScanBatch:
        """认领一次增量扫描并把差异写入台账。

        ``pages`` 为本次扫描抓到的页面快照，形如
        ``[{"page_key": "S4", "fields": {...}}, ...]``；
        ``baseline_pages`` 显式给出对照基线（兼容历史调用方），
        缺省时取该站点最近一个已确认批次，没有已确认批次时以空基线处理。
        同一站点同一输入快照重复提交（重启续跑）时返回原批次，
        原差异编号保持不变、持续可读，只累加认领次数。
        """
        baseline = (
            list(baseline_pages) if baseline_pages is not None
            else self._baseline_snapshot(target_site)
        )
        snapshot_hash = compute_snapshot_hash(
            target_site, cursor_id, pages, baseline)

        # 断点续跑：同一站点 + 同一基线 + 同一份输入快照只认一份有效批次。
        # 注意必须在建行之前查，否则新建行的哈希必然等于输入哈希，会把自己当成重复认领。
        existing = next(
            (row for row in self._batch_rows()
             if str(row.get("目标站点")) == target_site
             and str(row.get("扫描快照")) == snapshot_hash),
            None,
        )
        if existing is not None:
            existing["认领次数"] = int(existing.get("认领次数", 1) or 1) + 1
            return ScanBatch.from_row(
                existing, self._changes_of(str(existing.get("批次编号"))))

        start_cursor = self._site_read_position(target_site)
        batch_id = self._next_batch_id(target_site)
        now = _now()

        batch = ScanBatch(
            batch_id=batch_id,
            target_site=target_site,
            cursor_id=cursor_id,
            source_job=source_job,
            start_cursor=start_cursor,
            end_cursor=end_cursor or now,
            snapshot_hash=snapshot_hash,
            input_snapshot=list(pages),
            status=BATCH_PENDING,
            created_at=now,
        )
        batch.changes = self._diff_pages(batch, baseline, pages)

        batch_row = batch.to_row()
        batch_row["id"] = self._next_id(SCAN_BATCH_MODULE)
        self._batch_rows().append(batch_row)
        for change in batch.changes:
            change_row = change.to_row()
            change_row["id"] = self._next_id(SCAN_CHANGE_MODULE)
            change_row["pending"] = True
            change_row["abnormal"] = False
            self._change_rows().append(change_row)
        return batch

    def _diff_pages(self, batch: ScanBatch,
                    baseline: list[dict[str, Any]],
                    pages: list[dict[str, Any]]) -> list[ScanChange]:
        """对照基线算出新增/修改/删除，变动编号在重复认领间保持稳定。"""
        base_map = {str(item.get("page_key")): item for item in baseline}
        curr_map = {str(item.get("page_key")): item for item in pages}
        changes: list[ScanChange] = []

        for key in sorted(curr_map):
            current = curr_map[key]
            old = base_map.get(key)
            if old is None:
                change_type = SCAN_CHANGE_ADDED
                old_payload: dict[str, Any] = {}
            elif old.get("fields") != current.get("fields"):
                change_type = SCAN_CHANGE_UPDATED
                old_payload = dict(old.get("fields") or {})
            else:
                continue
            changes.append(ScanChange(
                change_id=self._change_id(batch.batch_id, key),
                batch_id=batch.batch_id,
                target_site=batch.target_site,
                page_key=key,
                change_type=change_type,
                old_payload=old_payload,
                new_payload=dict(current.get("fields") or {}),
                snapshot_hash=batch.snapshot_hash,
                detected_at=batch.created_at,
                status=CHANGE_PENDING,
            ))

        for key in sorted(base_map):
            if key in curr_map:
                continue
            old = base_map[key]
            changes.append(ScanChange(
                change_id=self._change_id(batch.batch_id, key),
                batch_id=batch.batch_id,
                target_site=batch.target_site,
                page_key=key,
                change_type=SCAN_CHANGE_REMOVED,
                old_payload=dict(old.get("fields") or {}),
                new_payload={},
                snapshot_hash=batch.snapshot_hash,
                detected_at=batch.created_at,
                status=CHANGE_PENDING,
            ))
        return changes

    # ------------------------------------------------------------------
    # 阶段 2：确认批次（进度落库 + 差异清单同步），同站点 FIFO
    # ------------------------------------------------------------------
    def confirm_batch(self, batch_id: str) -> ScanBatch:
        row = self._find_batch_row(batch_id)
        if row is None:
            raise KeyError(f"扫描批次 {batch_id} 不存在或已归档")
        if str(row.get("status")) != BATCH_PENDING:
            # 重复确认幂等：直接返回当前台账状态
            return ScanBatch.from_row(row, self._changes_of(batch_id))

        target_site = str(row.get("目标站点"))
        # 同站点确认优先级 FIFO：只允许确认该站点最早的待确认批次
        for other in self._batch_rows():
            if (str(other.get("目标站点")) == target_site
                    and str(other.get("status")) == BATCH_PENDING
                    and int(other.get("id", 0)) < int(row.get("id", 0))):
                raise RuntimeError(
                    f"站点 {target_site} 存在更早的待确认批次 "
                    f"{other.get('批次编号')}，需按确认顺序先处理前一批"
                )

        seq = self._next_confirm_seq(target_site)
        now = _now()
        row["确认序号"] = seq
        row["确认时间"] = now
        row["status"] = BATCH_CONFIRMED
        row["pending"] = True

        # 续扫游标随确认落库：写差异与进度确认现在是同一笔可追查记录
        self._advance_cursor(row, now)
        # 批次结论同步到差异检测清单，差异检测从这份批次确认记录取数
        self._sync_diff_checklist(row, now)
        for change_row in self._change_rows():
            if str(change_row.get("批次编号")) == batch_id:
                change_row["status"] = CHANGE_CONFIRMED
                change_row["pending"] = True
        return ScanBatch.from_row(row, self._changes_of(batch_id))

    def _advance_cursor(self, batch_row: dict[str, Any], now: str) -> None:
        cursor_id = str(batch_row.get("游标标记"))
        target_site = str(batch_row.get("目标站点"))
        for cursor_row in store.rows(INCR_MODULE):
            if (str(cursor_row.get("标记编号")) == cursor_id
                    or str(cursor_row.get("目标站点")) == target_site):
                cursor_row["上次游标"] = cursor_row.get("当前游标") or batch_row.get("起始游标")
                cursor_row["当前游标"] = batch_row.get("续扫游标")
                cursor_row["最后同步"] = now
                cursor_row["新增数量"] = batch_row.get("新增数量", 0)
                cursor_row["更新数量"] = batch_row.get("修改数量", 0)
                cursor_row["status"] = "已追上"
                cursor_row["pending"] = False
                return
        # 台账里没有对应游标标记时补登记一条，保证读位可独立追查
        rows = store.rows(INCR_MODULE)
        rows.append({
            "id": self._next_id(INCR_MODULE),
            "标记编号": cursor_id,
            "目标站点": target_site,
            "增量字段": "scan_batch",
            "上次游标": batch_row.get("起始游标", ""),
            "当前游标": batch_row.get("续扫游标", ""),
            "新增数量": batch_row.get("新增数量", 0),
            "更新数量": batch_row.get("修改数量", 0),
            "最后同步": now,
            "status": "已追上",
            "pending": False,
            "abnormal": False,
        })

    def _sync_diff_checklist(self, batch_row: dict[str, Any], now: str) -> None:
        batch_id = str(batch_row.get("批次编号"))
        existing = {
            str(row.get("记录编号")) for row in store.rows(DIFF_MODULE)
            if str(row.get("批次编号")) == batch_id
        }
        for change in self._changes_of(batch_id):
            if change.change_id in existing:
                continue
            rows = store.rows(DIFF_MODULE)
            rows.append({
                "id": self._next_id(DIFF_MODULE),
                "记录编号": change.change_id,
                "目标站点": change.target_site,
                "检测页面": change.page_key,
                "变更类型": change.change_type,
                "旧内容摘要": json.dumps(change.old_payload, ensure_ascii=False, sort_keys=True),
                "新内容摘要": json.dumps(change.new_payload, ensure_ascii=False, sort_keys=True),
                "影响字段": ",".join(sorted(change.new_payload or change.old_payload or {})),
                "检测时间": now,
                "批次编号": batch_id,
                "扫描快照": change.snapshot_hash,
                "status": "已确认",
                "pending": False,
                "abnormal": False,
            })

    # ------------------------------------------------------------------
    # 阶段 3：下游消费（实际发送新增/修改/删除），证据门禁 + 幂等
    # ------------------------------------------------------------------
    def next_deliverable(self, target_site: str | None = None) -> ScanBatch | None:
        """返回下一批获准读取的数据：已确认且未消费，按确认序号全局排队。"""
        candidates = [
            row for row in self._batch_rows()
            if str(row.get("status")) == BATCH_CONFIRMED
            and (target_site is None or str(row.get("目标站点")) == target_site)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda row: (int(row.get("确认序号", 0) or 0), int(row.get("id", 0))))
        row = candidates[0]
        return ScanBatch.from_row(row, self._changes_of(str(row.get("批次编号"))))

    def consume_batch(self, batch_id: str, consumer: str = "downstream") -> dict[str, Any]:
        """实际向下游发送本批获准读取的新增/修改/删除，并登记消费证据。

        门禁：同一站点前一批已确认但没有消费记录时，后一批不能被消费，
        即不能把后一批报成可跳过前批的进度。重复消费同一批次直接返回
        原有消费记录，变动不会重复发送。
        """
        row = self._find_batch_row(batch_id)
        if row is None:
            raise KeyError(f"扫描批次 {batch_id} 不存在或已归档")
        if str(row.get("status")) == BATCH_PENDING:
            raise RuntimeError(f"批次 {batch_id} 尚未确认，不能向下游发送")

        target_site = str(row.get("目标站点"))
        for other in self._batch_rows():
            if (str(other.get("目标站点")) == target_site
                    and str(other.get("批次编号")) != batch_id
                    and int(other.get("确认序号", 0) or 0) < int(row.get("确认序号", 0) or 0)
                    and not str(other.get("交付时间"))):
                raise RuntimeError(
                    f"站点 {target_site} 的前一批 {other.get('批次编号')} "
                    "尚无实际交付证据，不能跳过前批先消费本批"
                )

        # 消费幂等：同一批次只保留一份有效消费记录
        for record in self._consume_rows():
            if str(record.get("批次编号")) == batch_id:
                return dict(record)

        batch = ScanBatch.from_row(row, self._changes_of(batch_id))
        now = _now()
        record = {
            "id": self._next_id(CONSUME_MODULE),
            "消费编号": f"CONS-{batch_id}",
            "目标站点": target_site,
            "批次编号": batch_id,
            "扫描快照": batch.snapshot_hash,
            "消费端": consumer,
            "新增数量": batch.added_count,
            "修改数量": batch.updated_count,
            "删除数量": batch.removed_count,
            "发送明细": json.dumps(
                [
                    {
                        "change_id": c.change_id,
                        "page_key": c.page_key,
                        "change_type": c.change_type,
                    }
                    for c in batch.changes
                ],
                ensure_ascii=False,
            ),
            "消费时间": now,
            "status": "已交付",
            "pending": False,
            "abnormal": False,
        }
        self._consume_rows().append(record)

        row["交付时间"] = now
        row["status"] = BATCH_CONSUMED
        row["pending"] = False
        for change_row in self._change_rows():
            if str(change_row.get("批次编号")) == batch_id:
                change_row["status"] = CHANGE_DELIVERED
                change_row["pending"] = False
        return record

    def consumption_records(self, target_site: str | None = None) -> list[dict[str, Any]]:
        rows = self._consume_rows()
        if target_site:
            rows = [row for row in rows if str(row.get("目标站点")) == target_site]
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 断点续跑与站点读位
    # ------------------------------------------------------------------
    def site_position(self, target_site: str) -> dict[str, Any]:
        """独立站点的读位：只汇总该站点自己的批次，不受其它站点影响。"""
        rows = [
            row for row in self._batch_rows()
            if str(row.get("目标站点")) == target_site
        ]
        confirmed = [row for row in rows if row.get("确认序号") not in (None, "")]
        latest_confirmed = max(
            confirmed, key=lambda row: int(row.get("确认序号", 0) or 0), default=None
        )
        return {
            "目标站点": target_site,
            "读位游标": str(latest_confirmed.get("续扫游标", "")) if latest_confirmed else "",
            "已确认序号": int(latest_confirmed.get("确认序号", 0) or 0) if latest_confirmed else 0,
            "待确认批次": sum(1 for row in rows if str(row.get("status")) == BATCH_PENDING),
            "待消费批次": sum(1 for row in rows if str(row.get("status")) == BATCH_CONFIRMED),
            "已消费批次": sum(1 for row in rows if str(row.get("status")) == BATCH_CONSUMED),
            "最近批次": str(latest_confirmed.get("批次编号", "")) if latest_confirmed else "",
        }

    def resume_state(self) -> list[dict[str, Any]]:
        """工位刷新后的续跑视图：每个站点的读位、候选批次和下一步动作。"""
        sites = sorted({str(row.get("目标站点")) for row in self._batch_rows()})
        return [self.site_position(site) for site in sites]

    def changes_for_diff_checklist(self, target_site: str | None = None) -> list[ScanChange]:
        """差异检测清单取数口：数据全部来自批次确认记录，不再另算一份。"""
        changes: list[ScanChange] = []
        for row in self._change_rows():
            if target_site and str(row.get("目标站点")) != target_site:
                continue
            changes.append(ScanChange.from_row(row))
        return changes

    # ------------------------------------------------------------------
    # 通用动作入口：供统一资源路由复用
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        row = None
        for candidate in self._batch_rows():
            if int(candidate.get("id", 0)) == entry_id:
                row = candidate
                break
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        batch_id = str(row.get("批次编号"))
        try:
            if action == "确认批次":
                batch = self.confirm_batch(batch_id)
            elif action == "登记交付":
                self.consume_batch(batch_id)
                batch = self.get_batch(batch_id)
            else:
                return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        except RuntimeError as exc:
            return None, str(exc)
        assert batch is not None
        return batch.to_row(), f"{self.ENTITY}已{action}"

    # ------------------------------------------------------------------
    # 内部编号与基线
    # ------------------------------------------------------------------
    def _next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1

    def _next_batch_id(self, target_site: str) -> str:
        existing = [
            str(row.get("批次编号")) for row in self._batch_rows()
            if str(row.get("目标站点")) == target_site
        ]
        max_seq = 0
        for value in existing:
            tail = value.rsplit("-", 1)[-1]
            if tail.isdigit():
                max_seq = max(max_seq, int(tail))
        return f"SCAN-{target_site}-{max_seq + 1:03d}"

    def _next_confirm_seq(self, target_site: str) -> int:
        seqs = [
            int(row.get("确认序号", 0) or 0)
            for row in self._batch_rows()
            if str(row.get("目标站点")) == target_site and row.get("确认序号") not in (None, "")
        ]
        return (max(seqs) if seqs else 0) + 1

    def _change_id(self, batch_id: str, page_key: str) -> str:
        digest = hashlib.sha256(
            f"{batch_id}|{page_key}".encode("utf-8")
        ).hexdigest()[:10]
        return f"DIFF-{digest}"

    def _site_read_position(self, target_site: str) -> str:
        for cursor_row in store.rows(INCR_MODULE):
            if str(cursor_row.get("目标站点")) == target_site:
                return str(cursor_row.get("当前游标") or "")
        return ""

    def _baseline_snapshot(self, target_site: str) -> list[dict[str, Any]]:
        confirmed = [
            row for row in self._batch_rows()
            if str(row.get("目标站点")) == target_site and row.get("确认序号") not in (None, "")
        ]
        if not confirmed:
            return []
        latest = max(confirmed, key=lambda row: int(row.get("确认序号", 0) or 0))
        try:
            snapshot = json.loads(str(latest.get("输入快照") or "[]"))
        except (ValueError, TypeError):
            snapshot = []
        return snapshot if isinstance(snapshot, list) else []
