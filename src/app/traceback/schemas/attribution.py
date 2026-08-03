"""威胁归因 Schema

对齐 traceback_system_prd.md §6.2.5 AttributionResult + 02_phase2_design.md §3.6
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ActorFeatures(BaseModel):
    """从攻击链/证据提取的归因特征 (§3.2)"""
    ttps: list[str] = Field(default_factory=list, description="MITRE technique_id 集合")
    tools: list[str] = Field(default_factory=list, description="攻击工具指纹")
    infrastructure: list[str] = Field(default_factory=list, description="IOC: IP/域名")
    motive: str = Field("", description="推断动机")


class CandidateScore(BaseModel):
    """单个候选威胁组织评分 (§3.3 三因子加权)"""
    threat_actor: str
    confidence: float = Field(..., ge=0.0, le=100.0)
    score_ttp: float = 0.0
    score_ioc: float = 0.0
    score_tool: float = 0.0
    matched_ttps: list[str] = Field(default_factory=list)
    matched_iocs: list[str] = Field(default_factory=list)
    matched_tools: list[str] = Field(default_factory=list)


class ActorProfile(BaseModel):
    """攻击者画像 (§3.5, 对齐 PRD ActorProfile)"""
    name: str
    description: str = ""
    sophistication: str = ""
    primary_motivation: str = ""
    goals: list[str] = Field(default_factory=list)
    campaigns: list[str] = Field(default_factory=list)
    typical_ttps: list[str] = Field(default_factory=list)
    match_basis: str = Field("", description="本次匹配依据叙述")


class AttributionResult(BaseModel):
    """归因结果 (对齐 PRD §6.2.5)"""
    attribution_id: str
    task_id: str
    attributed: bool = False
    threat_actor: Optional[str] = None
    confidence_score: float = Field(0.0, ge=0.0, le=100.0)
    matched_iocs: list[dict[str, Any]] = Field(default_factory=list)
    matched_ttps: list[dict[str, Any]] = Field(default_factory=list)
    matched_campaigns: list[str] = Field(default_factory=list)
    similar_cases: list[dict[str, Any]] = Field(default_factory=list)
    actor_profile: Optional[ActorProfile] = None
    candidates: list[CandidateScore] = Field(default_factory=list, description="Top3 候选, 供审核修正")
    note: str = ""


class AttributionOut(BaseModel):
    task_id: str
    attribution: Optional[AttributionResult] = None
