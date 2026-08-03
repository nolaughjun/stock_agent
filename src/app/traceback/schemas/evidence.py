"""证据 Schema

对齐 traceback_system_prd.md §6.2.2 Evidence
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


EvidenceType = Literal["log", "traffic", "intel", "dns", "sample"]
ImportanceLevel = Literal["high", "medium", "low"]


class EvidenceMeta(BaseModel):
    """证据元数据 (存 MongoDB evidence_meta)"""
    evidence_id: str
    task_id: str
    evidence_type: EvidenceType
    source: str
    collect_time: datetime
    parsed_data: dict[str, Any] = Field(default_factory=dict)
    raw_summary: Optional[str] = Field(None, description="原始数据摘要(大字段存MinIO)")
    related_assets: list[str] = Field(default_factory=list)
    related_events: list[str] = Field(default_factory=list)
    importance: ImportanceLevel = "medium"
    tags: list[str] = Field(default_factory=list)
    annotations: list[dict[str, Any]] = Field(default_factory=list)
    unavailable: bool = Field(False, description="数据源不可达时标记降级")


class EvidenceRef(BaseModel):
    """证据引用 (存 PG evidences, 轻量)"""
    evidence_id: str
    task_id: str
    evidence_type: EvidenceType
    source: str
    importance: ImportanceLevel
    tags: list[str] = Field(default_factory=list)
    object_key: str
    meta_id: str
    collect_time: datetime

    model_config = {"from_attributes": True}


class EvidenceOut(BaseModel):
    """证据查询响应(合并引用+元数据)"""
    evidence_id: str
    task_id: str
    evidence_type: EvidenceType
    source: str
    importance: ImportanceLevel
    tags: list[str] = Field(default_factory=list)
    related_assets: list[str] = Field(default_factory=list)
    unavailable: bool = False
    parsed_data: dict[str, Any] = Field(default_factory=dict)
    raw_summary: Optional[str] = None
    collect_time: datetime


class QueryPlan(BaseModel):
    """LLM 生成的数据源查询计划 (§4.1)

    每个数据源一条结构化查询, 经白名单字段校验后执行, 防注入。
    """
    source: Literal["siem", "edr", "dns", "threat_intel"]
    query: dict[str, Any] = Field(..., description="数据源查询DSL")
    rationale: str = Field("", description="查询理由")


class EvidenceScore(BaseModel):
    """LLM 证据评估打分 (§4.1)"""
    evidence_id: str
    importance: ImportanceLevel
    tags: list[str] = Field(default_factory=list)
    related_assets: list[str] = Field(default_factory=list)
    summary: str = Field("", description="一句话摘要")


class CollectContext(BaseModel):
    """证据收集上下文 (适配器统一输入)"""
    task_id: str
    asset_ips: list[str] = Field(default_factory=list)
    asset_hosts: list[str] = Field(default_factory=list)
    asset_users: list[str] = Field(default_factory=list)
    event_start_time: Optional[datetime] = None
    event_end_time: Optional[datetime] = None
    # LLM 生成的数据源查询提示 (供适配器构造 DSL)
    query_hint: dict[str, Any] = Field(default_factory=dict)


class RawEvidence(BaseModel):
    """适配器返回的原始证据项 (统一结构, 收集前)"""
    evidence_type: EvidenceType
    source: str
    raw: dict[str, Any] = Field(default_factory=dict)
    parsed: dict[str, Any] = Field(default_factory=dict)
    related_assets: list[str] = Field(default_factory=list)
    importance: ImportanceLevel = "medium"
