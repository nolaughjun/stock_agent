"""时间线事件 Schema

对齐 traceback_system_prd.md §6.2.3 TimelineEvent + 01_phase1_design.md §4.2
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# 关键事件类型对齐 MITRE ATT&CK 战术 (§4.2)
EventCategory = Literal[
    "initial_access", "execution", "persistence", "privilege_escalation",
    "defense_evasion", "credential_access", "discovery", "lateral_movement",
    "collection", "exfiltration", "impact", "unknown",
]
SeverityLevel = Literal["critical", "high", "medium", "low", "info"]


class TimelineEvent(BaseModel):
    """时间线事件 (存 MongoDB timeline_events)"""
    event_id: str
    task_id: str
    event_time: datetime
    event_type: str
    event_category: EventCategory
    title: str
    description: str = ""
    severity: SeverityLevel = "medium"
    source_evidence: list[str] = Field(default_factory=list, description="来源证据ID")
    related_events: list[str] = Field(default_factory=list)
    attack_tactics: list[str] = Field(default_factory=list)
    attack_techniques: list[str] = Field(default_factory=list, description="MITRE Technique ID")
    is_key_event: bool = False
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    verified: bool = False


class KeyEventMark(BaseModel):
    """LLM 关键节点标记结果 (§4.2)"""
    event_id: str
    is_key_event: bool
    reason: str = ""


class TimelineOut(BaseModel):
    task_id: str
    events: list[TimelineEvent]
    key_event_count: int = 0
