"""批次消费台账：记录每次向下游实际发送的站点、批次与扫描快照。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class BatchConsumeService(BaseModuleService):
    MODULE = "batch_consume"
    ENTITY = "消费记录"
    OPERATION_TERM = "消费"
    LIST_FIELDS = [
        "消费编号", "目标站点", "批次编号", "扫描快照", "消费端",
        "新增数量", "修改数量", "删除数量", "发送明细", "消费时间",
    ]
    REQUIRED_FIELDS = ["消费编号", "目标站点", "批次编号"]
    STATUS_ORDER = ["已交付"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS = []
