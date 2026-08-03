"""LangGraph 全局状态定义

对齐 01_phase1_design.md §3.2 TracebackState
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict
from operator import add


Stage = Literal[
    "created", "collecting", "timeline_built",
    "review_timeline", "chain_analyzed",
    "review_chain", "attributed", "review_attribution", "reported",
]


class TracebackState(TypedDict, total=False):
    """溯源分析全局状态

    累积型字段(evidence_ids/errors/human_reviews)用 add reducer, 跨节点追加。
    """
    task_id: str
    task: dict[str, Any]                       # TracebackTask 快照
    evidence_ids: Annotated[list[str], add]    # 累积证据ID
    timeline_events: list[dict[str, Any]]      # TimelineEvent[]
    attack_chain: dict[str, Any] | None        # AttackChain
    attribution: dict[str, Any] | None         # AttributionResult (P2 完整)
    similar_cases: list[dict[str, Any]]        # 相似案例匹配结果
    deep_evidence_ids: Annotated[list[str], add]  # 深度取证产出证据 (P2)
    report_path: str | None
    stage: Stage
    errors: Annotated[list[str], add]
    human_reviews: Annotated[list[dict[str, Any]], add]
    # 人工审核恢复时由 API 注入的决策
    review_decision: dict[str, Any] | None


# 各阶段进度百分比 (对齐 TaskProgress.progress_percent)
STAGE_PROGRESS: dict[Stage, int] = {
    "created": 5,
    "collecting": 20,
    "timeline_built": 40,
    "review_timeline": 45,
    "chain_analyzed": 65,
    "review_chain": 70,
    "attributed": 83,
    "review_attribution": 88,
    "reported": 100,
}

AWAITING_REVIEW_STAGES: set[Stage] = {
    "review_timeline", "review_chain", "review_attribution",
}
