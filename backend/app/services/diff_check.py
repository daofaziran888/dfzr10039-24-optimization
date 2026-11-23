"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class DiffCheckService(BaseModuleService):
    MODULE = "diff_check"
    ENTITY = "差异记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["记录编号", "目标站点", "检测页面", "变更类型", "旧内容摘要", "新内容摘要", "影响字段", "批次编号", "扫描快照", "检测时间"]
    REQUIRED_FIELDS = ["记录编号", "目标站点", "检测页面"]
    STATUS_ORDER = ["已检测", "核实中", "已确认", "可忽略"]
    ACTION_RULES = {"发起核实": "核实中", "确认变更": "已确认", "忽略差异": "可忽略"}
    NEGATIVE_ACTIONS = ["忽略差异"]
