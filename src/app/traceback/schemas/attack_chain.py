"""攻击链 Schema

对齐 traceback_system_prd.md §6.2.4 AttackChain + 01_phase1_design.md §4.3
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class EntryPoint(BaseModel):
    """攻击入口点 (§4.3 反向溯源)"""
    asset: str
    vector: str = Field("", description="入侵向量, 如钓鱼邮件/RDP爆破")
    technique_id: Optional[str] = Field(None, description="MITRE Technique ID")
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0.0, le=1.0)


class LateralMovement(BaseModel):
    """横向移动路径"""
    from_asset: str
    to_asset: str
    technique_id: Optional[str] = None
    technique_label: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class AttackPhase(BaseModel):
    tactic: str
    technique_id: Optional[str] = None
    event_ids: list[str] = Field(default_factory=list)
    description: str = ""


class AttackPathNode(BaseModel):
    id: str
    type: Literal["victim", "attacker", "pivot"] = "victim"


class AttackPathEdge(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    technique: Optional[str] = None
    label: str = ""


class AttackChain(BaseModel):
    """攻击链 (存 PG attack_chains.payload JSONB)"""
    chain_id: str
    task_id: str
    phases: list[AttackPhase] = Field(default_factory=list)
    entry_point: Optional[EntryPoint] = None
    lateral_movements: list[LateralMovement] = Field(default_factory=list)
    affected_assets: list[str] = Field(default_factory=list)
    data_exfiltration: bool = False
    attacker_ips: list[str] = Field(default_factory=list)
    attacker_domains: list[str] = Field(default_factory=list)
    attacker_tools: list[str] = Field(default_factory=list)
    # 攻击路径图 (§4.3): 节点=资产, 边=攻击动作
    path_graph: dict[str, Any] = Field(
        default_factory=lambda: {"nodes": [], "edges": []}
    )


class TTPMatch(BaseModel):
    """LLM TTP 标注结果 (§4.3)"""
    event_id: str
    tactic: str
    technique_id: Optional[str] = None  # 须命中 ATT&CK 白名单, 否则 None
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    note: str = ""


class AttackChainOut(BaseModel):
    task_id: str
    chain: Optional[AttackChain] = None
