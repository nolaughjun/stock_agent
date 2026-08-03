"""图节点封装

对齐 01_phase1_design.md §3.1 状态机 / §3.4 interrupt 审核中断-恢复

M1 骨架: 业务智能体节点为 stub(仅推进 stage), 真实逻辑 S2~S4 填充。
interrupt 审核机制为真实实现, 是 P1 编排的核心, 骨架即需可用。
"""
from __future__ import annotations

from typing import Any, Callable

from langgraph.types import interrupt

from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.state import TracebackState

# 节点函数签名: (state) -> partial state
NodeFn = Callable[[TracebackState], dict[str, Any]]


# ---- 业务节点 stub (S2~S4 替换为真实智能体) ----

async def collect_evidence_node(state: TracebackState) -> dict[str, Any]:
    """证据收集 (§4.1). S2: EvidenceCollector 智能体 (LLM 查询计划 + 并行适配器 + 评估)。"""
    from src.app.traceback.agents.evidence_collector import EvidenceCollector

    logger.info("[{}] 证据收集 task={}", "collect_evidence", state.get("task_id"))
    agent = EvidenceCollector()
    return await agent.safe_run(state)


async def collect_evidence_stub(state: TracebackState) -> dict[str, Any]:
    """证据收集 stub (测试用, 不触达 DB/适配器)"""
    logger.info("[{}] 证据收集(stub) task={}", "collect_evidence", state.get("task_id"))
    return {"stage": "collecting", "evidence_ids": []}


async def build_timeline_node(state: TracebackState) -> dict[str, Any]:
    """时间线构建 (§4.2). S3: TimelineBuilder 智能体。"""
    from src.app.traceback.agents.timeline_builder import TimelineBuilder

    logger.info("[{}] 时间线构建 task={}", "build_timeline", state.get("task_id"))
    agent = TimelineBuilder()
    return await agent.safe_run(state)


async def build_timeline_stub(state: TracebackState) -> dict[str, Any]:
    """时间线构建 stub (测试用)"""
    logger.info("[{}] 时间线构建(stub) task={}", "build_timeline", state.get("task_id"))
    return {"stage": "timeline_built", "timeline_events": []}


async def analyze_chain_node(state: TracebackState) -> dict[str, Any]:
    """攻击链分析 (§4.3). S3: AttackChainAgent 智能体。"""
    from src.app.traceback.agents.attack_chain import AttackChainAgent

    logger.info("[{}] 攻击链分析 task={}", "analyze_chain", state.get("task_id"))
    agent = AttackChainAgent()
    return await agent.safe_run(state)


async def analyze_chain_stub(state: TracebackState) -> dict[str, Any]:
    """攻击链分析 stub (测试用)"""
    logger.info("[{}] 攻击链分析(stub) task={}", "analyze_chain", state.get("task_id"))
    return {"stage": "chain_analyzed", "attack_chain": None}


async def attribute_node(state: TracebackState) -> dict[str, Any]:
    """威胁归因 (P1 桩, §1.2). P2 替换为完整归因智能体。

    P1 仅汇总 IOC 命中, 置信度 0, 保证流水线结构完整。
    """
    logger.info("[{}] 归因(P1桩) task={}", "attribute", state.get("task_id"))
    return {
        "stage": "attributed",
        "attribution": {
            "attributed": False,
            "confidence_score": 0.0,
            "matched_iocs": [],
            "note": "P1 桩: 归因能力 P2 实现",
        },
    }


async def report_node(state: TracebackState) -> dict[str, Any]:
    """报告生成 (§4.4). S4: Reporter 智能体。"""
    from src.app.traceback.agents.reporter import Reporter

    logger.info("[{}] 报告生成 task={}", "report", state.get("task_id"))
    agent = Reporter()
    return await agent.safe_run(state)


async def report_stub(state: TracebackState) -> dict[str, Any]:
    """报告生成 stub (测试用)"""
    logger.info("[{}] 报告生成(stub) task={}", "report", state.get("task_id"))
    return {"stage": "reported", "report_path": None}


# ---- 人工审核 interrupt 节点 (真实实现) ----

def _review(stage_name: str, next_approve: str, next_revise: str) -> NodeFn:
    """生成审核节点: interrupt 暂停等待人工决策, 恢复后据 action 路由。

    Args:
        stage_name: review_timeline / review_chain
        next_approve: approve 后推进到的 stage
        next_revise:  revise 后回退到的 stage
    """

    async def _node(state: TracebackState) -> dict[str, Any]:
        decision = interrupt({
            "stage": stage_name,
            "task_id": state.get("task_id"),
            "timeline": state.get("timeline_events"),
            "attack_chain": state.get("attack_chain"),
            "prompt": f"请审核 {stage_name}, approve 继续 或 revise 修正",
        })
        # 恢复时由 API 注入 decision: {action, comments, modifications, reviewer}
        action = decision.get("action", "approve")
        logger.info(
            "[{}] 审核决策 action={} reviewer={}",
            stage_name, action, decision.get("reviewer"),
        )
        next_stage = next_approve if action == "approve" else next_revise
        return {
            "stage": next_stage,
            "human_reviews": [decision],
            "review_decision": decision,
        }

    return _node


review_timeline_node = _review("timeline", "chain_analyzed", "collecting")
review_chain_node = _review("attack_chain", "attributed", "chain_analyzed")


# ---- 条件路由 ----

def route_after_review_timeline(state: TracebackState) -> str:
    """审核①后: approve → analyze_chain; revise → collect_evidence"""
    decision = state.get("review_decision") or {}
    return "analyze_chain" if decision.get("action") == "approve" else "collect_evidence"


def route_after_review_chain(state: TracebackState) -> str:
    """审核②后: approve → attribute; revise → analyze_chain"""
    decision = state.get("review_decision") or {}
    return "attribute" if decision.get("action") == "approve" else "analyze_chain"


# 默认业务节点注册表 (审核 interrupt 节点固定, 不可覆盖)
DEFAULT_NODES: dict[str, NodeFn] = {
    "collect_evidence": collect_evidence_node,
    "build_timeline": build_timeline_node,
    "analyze_chain": analyze_chain_node,
    "attribute": attribute_node,
    "report": report_node,
}
