"""人工审核 Schema

对齐 traceback_system_prd.md §6.2.1 HumanReview + 01_phase1_design.md §3.4
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


ReviewStage = Literal["timeline", "attack_chain", "report", "attribution", "knowledge"]
ReviewAction = Literal["approve", "revise"]


class ReviewRequest(BaseModel):
    """提交人工审核 (POST /tasks/{id}/review)"""
    stage: ReviewStage
    action: ReviewAction
    reviewer: str = "anonymous"
    comments: Optional[str] = None
    modifications: list[dict[str, Any]] = Field(
        default_factory=list, description="修正项, 如补充事件/修正TTP"
    )


class HumanReview(BaseModel):
    """审核记录 (存 PG human_reviews)"""
    review_id: str
    task_id: str
    stage: ReviewStage
    action: ReviewAction
    reviewer: str
    comments: Optional[str] = None
    modifications: list[dict[str, Any]] = Field(default_factory=list)
    review_time: datetime

    model_config = {"from_attributes": True}


class ResumeRequest(BaseModel):
    """恢复被中断的分析 (POST /tasks/{id}/resume)

    P1 实现方式: LangGraph Command(resume=decision), decision 即审核结果。
    若已通过 /review 提交, 此处仅需触发 resume。
    """
    decision: Optional[dict[str, Any]] = Field(
        None, description="可选, 直接携带审核决策; 否则用最近一次 /review 结果"
    )
