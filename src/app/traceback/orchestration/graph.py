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
    analyze_chain_node,
    attribute_node,
    build_timeline_node,
    collect_evidence_node,
    report_node,
    review_chain_node,
    review_timeline_node,
    route_after_review_chain,
    route_after_review_timeline,
)
from src.app.traceback.orchestration.state import TracebackState


def build_graph(checkpointer: Any = None):
    """构建溯源分析状态图。

    Args:
        checkpointer: LangGraph checkpointer (MemorySaver 默认; 生产用 PG, 对齐 §5.1)
    """
    if checkpointer is None:
        checkpointer = MemorySaver()

    graph = StateGraph(TracebackState)

    # 业务节点
    graph.add_node("collect_evidence", collect_evidence_node)
    graph.add_node("build_timeline", build_timeline_node)
    graph.add_node("review_timeline", review_timeline_node)
    graph.add_node("analyze_chain", analyze_chain_node)
    graph.add_node("review_chain", review_chain_node)
    graph.add_node("attribute", attribute_node)
    graph.add_node("report", report_node)

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

    graph.add_edge("attribute", "report")
    graph.add_edge("report", END)

    return graph.compile(checkpointer=checkpointer)


# 全局编译图 (懒加载)
_compiled: Any = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


def reset_graph() -> None:
    """测试用: 重置编译图"""
    global _compiled
    _compiled = None
