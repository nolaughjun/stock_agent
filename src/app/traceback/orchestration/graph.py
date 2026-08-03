"""LangGraph 状态图定义

对齐 01_phase1_design.md §3.1 编排状态机

状态机:
  START → collect_evidence → build_timeline → review_timeline(interrupt)
        → [approve] analyze_chain → review_chain(interrupt)
        → [approve] attribute → report → END
        (revise 回退到对应前置节点)

interrupt 审核节点暂停后, 由 API 层 POST /resume 携带决策,
调用 graph.astream(Command(resume=decision)) 恢复。
"""
from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.app.traceback.orchestration.nodes import (
    DEFAULT_NODES,
    review_attribution_node,
    review_chain_node,
    review_timeline_node,
    route_after_review_attribution,
    route_after_review_chain,
    route_after_review_timeline,
)
from src.app.traceback.orchestration.state import TracebackState


def build_graph(
    checkpointer: Any = None,
    node_overrides: dict[str, Any] | None = None,
):
    """构建溯源分析状态图。

    Args:
        checkpointer: LangGraph checkpointer (MemorySaver 默认; 生产用 PG, 对齐 §5.1)
        node_overrides: 业务节点覆盖 (测试用 stub, 审核节点固定不可覆盖)
    """
    if checkpointer is None:
        checkpointer = MemorySaver()

    nodes = {**DEFAULT_NODES, **(node_overrides or {})}

    graph = StateGraph(TracebackState)

    # 业务节点 (可覆盖)
    graph.add_node("collect_evidence", nodes["collect_evidence"])
    graph.add_node("build_timeline", nodes["build_timeline"])
    graph.add_node("review_timeline", review_timeline_node)
    graph.add_node("analyze_chain", nodes["analyze_chain"])
    graph.add_node("review_chain", review_chain_node)
    graph.add_node("attribute", nodes["attribute"])
    graph.add_node("review_attribution", review_attribution_node)
    graph.add_node("report", nodes["report"])

    # 边
    graph.add_edge(START, "collect_evidence")
    graph.add_edge("collect_evidence", "build_timeline")
    graph.add_edge("build_timeline", "review_timeline")

    # 审核①: approve → analyze_chain; revise → collect_evidence
    graph.add_conditional_edges(
        "review_timeline",
        route_after_review_timeline,
        {"analyze_chain": "analyze_chain", "collect_evidence": "collect_evidence"},
    )

    graph.add_edge("analyze_chain", "review_chain")

    # 审核②: approve → attribute; revise → analyze_chain
    graph.add_conditional_edges(
        "review_chain",
        route_after_review_chain,
        {"attribute": "attribute", "analyze_chain": "analyze_chain"},
    )

    # 归因后插入审核③
    graph.add_edge("attribute", "review_attribution")

    # 审核③: approve → report; revise → attribute(重算归因)
    graph.add_conditional_edges(
        "review_attribution",
        route_after_review_attribution,
        {"report": "report", "attribute": "attribute"},
    )

    graph.add_edge("report", END)

    return graph.compile(checkpointer=checkpointer)


# 全局编译图 (懒加载)
_compiled: Any = None
_checkpointer: Any = None


def set_checkpointer(checkpointer: Any) -> None:
    """设置检查点后端 (由 lifespan 调用), 并重置编译图使其生效"""
    global _checkpointer, _compiled
    _checkpointer = checkpointer
    _compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph(checkpointer=_checkpointer)
    return _compiled


def reset_graph() -> None:
    """测试用: 重置编译图"""
    global _compiled
    _compiled = None
