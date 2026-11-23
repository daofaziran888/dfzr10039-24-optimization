"""增量扫描批次确认专用路由：认领、确认、消费、续跑读位都落在同一份批次台账。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.scan_batch import ScanBatchService

router = APIRouter(prefix="/api/scan-batches", tags=["增量扫描批次确认"])
service = ScanBatchService()


class ClaimPayload(BaseModel):
    target_site: str
    cursor_id: str
    pages: list[dict[str, Any]] = Field(default_factory=list)
    source_job: str = ""
    end_cursor: str = ""


def _batch_payload(batch) -> dict[str, Any]:
    row = batch.to_row()
    row["changes"] = [change.to_row() for change in batch.changes]
    return row


@router.post("/claim")
def claim_scan(payload: ClaimPayload) -> dict[str, Any]:
    """认领扫描：写差异并留输入快照；同站点同输入重复提交返回原批次。"""
    if not payload.target_site.strip() or not payload.cursor_id.strip():
        raise HTTPException(status_code=422, detail="目标站点和游标标记不能为空")
    batch = service.claim_scan(
        target_site=payload.target_site.strip(),
        cursor_id=payload.cursor_id.strip(),
        pages=payload.pages,
        source_job=payload.source_job,
        end_cursor=payload.end_cursor,
    )
    return {"ok": True, "message": "扫描批次已认领", "batch": _batch_payload(batch)}


@router.post("/{batch_id}/confirm")
def confirm_batch(batch_id: str) -> dict[str, Any]:
    """确认批次：续扫游标落库并把结论同步到差异检测清单。"""
    try:
        batch = service.confirm_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "message": "批次已确认，读位已推进", "batch": _batch_payload(batch)}


@router.get("/next-deliverable")
def next_deliverable(target_site: str | None = None) -> dict[str, Any]:
    """下一批获准读取的数据：按确认序号排队，独立站点互不干扰。"""
    batch = service.next_deliverable(target_site)
    if batch is None:
        return {"ok": True, "message": "暂无待消费批次", "batch": None}
    return {"ok": True, "message": "返回下一批可消费数据", "batch": _batch_payload(batch)}


@router.post("/{batch_id}/consume")
def consume_batch(batch_id: str, consumer: str = "downstream") -> dict[str, Any]:
    """实际发送本批新增/修改/删除并登记消费证据；重复消费幂等。"""
    try:
        record = service.consume_batch(batch_id, consumer=consumer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "message": "批次已交付下游", "consumption": record}


@router.get("/resume")
def resume_state() -> dict[str, Any]:
    """工位刷新后的断点续跑视图：各站点候选批次、确认优先级和下一步。"""
    return {"ok": True, "positions": service.resume_state()}


@router.get("/positions/{target_site}")
def site_position(target_site: str) -> dict[str, Any]:
    """独立站点的确认位置单独可查，不跟随其它站点变化。"""
    return {"ok": True, "position": service.site_position(target_site)}


@router.get("/diff-checklist")
def diff_checklist(target_site: str | None = None) -> dict[str, Any]:
    """差异检测清单取数口：从同一份批次确认记录读取。"""
    items = [change.to_row() for change in service.changes_for_diff_checklist(target_site)]
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/consumptions")
def consumptions(target_site: str | None = None) -> dict[str, Any]:
    """消费证据清单：每次消费的站点、批次与扫描快照。"""
    items = service.consumption_records(target_site)
    return {"ok": True, "items": items, "total": len(items)}
