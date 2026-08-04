"""知识库 Schema (P3)

对齐 traceback_system_prd.md §6.2.6~§6.2.9 + 03_phase3_design.md §3
五大库: KnowledgeCase / IOCKnowledge / TTPKnowledge / ResponsePlaybook / KnowledgeArticle
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


CaseStatus = Literal["draft", "published", "archived"]
AttackType = Literal["ransomware", "apt", "data_leak", "phishing", "lateral_movement", "cryptomining", "other"]
IOCType = Literal["ip", "domain", "url", "hash", "email"]
SeverityLevel = Literal["critical", "high", "medium", "low", "info"]


class KnowledgeCase(BaseModel):
    """知识案例 (§6.2.6, Mongo + 向量)"""
    case_id: str
    source_task_id: Optional[str] = None
    title: str
    description: str = ""
    attack_type: AttackType = "other"
    severity: SeverityLevel = "medium"
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    mitre_attack_ids: list[str] = Field(default_factory=list)
    iocs: list[dict[str, Any]] = Field(default_factory=list)
    ttps: list[str] = Field(default_factory=list)
    response_actions: list[str] = Field(default_factory=list)
    lessons_learned: str = ""
    author: str = "system"
    reviewer: Optional[str] = None
    status: CaseStatus = "draft"
    view_count: int = 0
    reference_count: int = 0
    rating: float = 0.0
    create_time: datetime = Field(default_factory=datetime.now)
    update_time: datetime = Field(default_factory=datetime.now)


class IOCKnowledge(BaseModel):
    """IOC 知识条目 (§6.2.7, Neo4j 图谱 + PG 元数据)"""
    ioc_id: str
    source_case_id: Optional[str] = None
    ioc_type: IOCType
    ioc_value: str
    threat_type: str = ""
    malware_family: Optional[str] = None
    threat_actor: Optional[str] = None
    campaign: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    context: str = ""
    confidence: float = 0.5
    false_positive: bool = False
    tags: list[str] = Field(default_factory=list)


class TTPKnowledge(BaseModel):
    """TTP 知识 (§6.2.6 ttps + F-007-06, PG + MITRE 关联)"""
    ttp_id: str                          # 如 T1566.001
    mitre_tactic: str
    mitre_technique: str
    name: str
    description: str = ""
    detection_tips: list[str] = Field(default_factory=list)
    response_tips: list[str] = Field(default_factory=list)
    source_cases: list[str] = Field(default_factory=list)


class ResponseStep(BaseModel):
    step_id: str
    order: int
    action_type: Literal["isolate", "investigate", "eradicate", "recover", "harden", "monitor"]
    description: str
    commands: list[str] = Field(default_factory=list)
    expected_result: str = ""
    timeout: int = 300


class ResponsePlaybook(BaseModel):
    """处置方案 (§6.2.8, PG)"""
    playbook_id: str
    source_case_id: Optional[str] = None
    name: str
    description: str = ""
    attack_type: AttackType
    steps: list[ResponseStep] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    applicable_assets: list[str] = Field(default_factory=list)
    effectiveness: float = 0.0
    side_effects: list[str] = Field(default_factory=list)
    author: str = "system"
    version: str = "1.0"
    usage_count: int = 0
    success_rate: float = 0.0


class KnowledgeArticle(BaseModel):
    """知识文章 (§6.2.9, Mongo + 向量)"""
    article_id: str
    title: str
    content: str = ""
    category: Literal["methodology", "technique", "tool", "best_practice"] = "methodology"
    tags: list[str] = Field(default_factory=list)
    related_cases: list[str] = Field(default_factory=list)
    author: str = "system"
    status: CaseStatus = "draft"
    view_count: int = 0
    like_count: int = 0
    rating: float = 0.0


class KnowledgeSinkResult(BaseModel):
    """知识沉淀结果"""
    case_id: Optional[str] = None
    ioc_ids: list[str] = Field(default_factory=list)
    ttp_refs: list[str] = Field(default_factory=list)
    playbook_id: Optional[str] = None
    note: str = ""


class PlaybookRecommend(BaseModel):
    playbook_id: str
    name: str
    attack_type: AttackType
    match_score: float
    usage_count: int = 0
    success_rate: float = 0.0


class QAAnswer(BaseModel):
    """RAG 问答回答 (强制引用)"""
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    has_result: bool = True
