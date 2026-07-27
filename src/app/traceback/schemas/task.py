"""溯源任务 Schema

对齐 traceback_system_prd.md §6.2.1 TracebackTask + 01_phase1_design.md §5.1
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


TaskStatus = Literal["created", "analyzing", "reviewing", "done", "failed"]
AnalysisStage = Literal[
    "created", "collecting", "timeline_built",
    "review_timeline", "chain_analyzed",
    "review_chain", "attributed", "reported",
]


class TaskCreate(BaseModel):
    """创建任务请求 (US-001)"""
    task_name: str = Field(..., min_length=1, max_length=200, description="任务名称")
    description: Optional[str] = Field(None, description="事件描述")
    alert_ids: list[str] = Field(default_factory=list, description="关联告警ID")
    asset_ips: list[str] = Field(default_factory=list, description="涉及IP")
    asset_hosts: list[str] = Field(default_factory=list, description="涉及主机")
    asset_users: list[str] = Field(default_factory=list, description="涉及用户账号")
    event_start_time: Optional[datetime] = Field(None, description="事件开始时间")
    event_end_time: Optional[datetime] = Field(None, description="事件结束时间")
    assignee: Optional[str] = Field(None, description="分配分析师")


class TaskUpdate(BaseModel):
    task_name: Optional[str] = None
    description: Optional[str] = None
    asset_ips: Optional[list[str]] = None
    asset_hosts: Optional[list[str]] = None
    asset_users: Optional[list[str]] = None
    event_start_time: Optional[datetime] = None
    event_end_time: Optional[datetime] = None
    assignee: Optional[str] = None
    status: Optional[TaskStatus] = None


class TaskOut(BaseModel):
    """任务响应"""
    task_id: str
    task_name: str
    description: Optional[str] = None
    status: TaskStatus
    current_stage: AnalysisStage
    alert_ids: list[str] = Field(default_factory=list)
    asset_ips: list[str] = Field(default_factory=list)
    asset_hosts: list[str] = Field(default_factory=list)
    asset_users: list[str] = Field(default_factory=list)
    event_start_time: Optional[datetime] = None
    event_end_time: Optional[datetime] = None
    assignee: Optional[str] = None
    retry_count: int = 0
    error_message: Optional[str] = None
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}


class TaskListOut(BaseModel):
    total: int
    items: list[TaskOut]


class TaskProgress(BaseModel):
    """分析进度 (GET /progress)"""
    task_id: str
    status: TaskStatus
    current_stage: AnalysisStage
    progress_percent: int = Field(..., ge=0, le=100, description="进度百分比")
    awaiting_review: bool = Field(False, description="是否等待人工审核")
    review_stage: Optional[Literal["timeline", "attack_chain", "report"]] = None
    message: Optional[str] = None
