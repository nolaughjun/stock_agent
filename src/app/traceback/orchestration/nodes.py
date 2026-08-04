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

# conditional edge 返回值: 审核④ approve → 结束
END_NODE = "done"

# 节点函数签名: (state) -> partial state
NodeFn = Callable[[TracebackState], dict[str, Any]]


# ---- 业务节点 stub (S2~S4 替换为真实智能体) ----

async def collect_evidence_node(state: TracebackState) -> dict[str, Any]:
    """证据收集 (§4.1). 基础证据 + 深度取证 + P3 IOC 自动匹配(旁路注入)。"""
    from src.app.traceback.agents.evidence_collector import EvidenceCollector
    from src.app.traceback.services.deep_forensics import DeepForensicsCollector

    logger.info("[{}] 证据收集 task={}", "collect_evidence", state.get("task_id"))
    basic = await EvidenceCollector().safe_run(state)
    deep = await DeepForensicsCollector().run(state)
    # P3 旁路注入: IOC 自动匹配 (F-008-03), best-effort 不阻塞
    suggestion = await _knowledge_ioc_match(state)
    return {
        **basic,
        "deep_evidence_ids": deep.get("deep_evidence_ids", []),
        **({"knowledge_suggestion": suggestion} if suggestion else {}),
    }


async def _knowledge_ioc_match(state: TracebackState) -> dict[str, Any] | None:
    """IOC 自动匹配: 任务资产 → 查知识库 IOC → 命中标注 (F-008-03)"""
    try:
        from src.app.traceback.core.db import session_scope
        from src.app.traceback.repositories import knowledge_repo, task_repo

        task_id = state.get("task_id")
        async with session_scope() as session:
            task = await task_repo.get_task(session, task_id)
        if not task:
            return None
        iocs = [{"type": "ip", "value": ip} for ip in (task.get("asset_ips") or [])]
        if not iocs:
            return None
        async with session_scope() as session:
            matches = await knowledge_repo.batch_query_iocs(session, iocs)
        hits = [m for m in matches if m.get("found")]
        return {"ioc_matches": hits} if hits else None
    except Exception as e:  # noqa: BLE001
        logger.debug("IOC 自动匹配跳过: {}", str(e)[:80])
        return None


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
    """威胁归因 (§3). P2: AttributionAgent 智能体 (特征提取/三因子/画像)。"""
    from src.app.traceback.agents.attribution import AttributionAgent

    logger.info("[{}] 归因分析 task={}", "attribute", state.get("task_id"))
    agent = AttributionAgent()
    return await agent.safe_run(state)


async def attribute_stub(state: TracebackState) -> dict[str, Any]:
    """归因 stub (测试用)"""
    logger.info("[{}] 归因(stub) task={}", "attribute", state.get("task_id"))
    return {
        "stage": "attributed",
        "attribution": {
            "attributed": False, "confidence_score": 0.0, "matched_iocs": [],
            "note": "stub",
        },
        "similar_cases": [],
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


async def knowledge_sink_node(state: TracebackState) -> dict[str, Any]:
    """知识沉淀 (§6). P3: KnowledgeSink 自动提取案例/IOC/TTP/Playbook 草稿。"""
    from src.app.traceback.agents.knowledge_sink import KnowledgeSink

    logger.info("[{}] 知识沉淀 task={}", "knowledge_sink", state.get("task_id"))
    agent = KnowledgeSink()
    return await agent.safe_run(state)


async def knowledge_sink_stub(state: TracebackState) -> dict[str, Any]:
    """知识沉淀 stub (测试用)"""
    logger.info("[{}] 知识沉淀(stub) task={}", "knowledge_sink", state.get("task_id"))
    return {"stage": "knowledge_sunk", "pending_knowledge": {"case_id": None, "note": "stub"}}


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
            "attribution": state.get("attribution"),
            "similar_cases": state.get("similar_cases"),
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
# 审核③: 归因 approve → reported(报告); revise → 重算归因
review_attribution_node = _review("attribution", "reported", "attributed")
# 审核④: 知识沉淀 approve → done; revise → 重新沉淀
review_knowledge_node = _review("knowledge", "done", "knowledge_sunk")


# ---- 条件路由 ----

def route_after_review_timeline(state: TracebackState) -> str:
    """审核①后: approve → analyze_chain; revise → collect_evidence"""
    decision = state.get("review_decision") or {}
    return "analyze_chain" if decision.get("action") == "approve" else "collect_evidence"


def route_after_review_chain(state: TracebackState) -> str:
    """审核②后: approve → attribute; revise → analyze_chain"""
    decision = state.get("review_decision") or {}
    return "attribute" if decision.get("action") == "approve" else "analyze_chain"


def route_after_review_attribution(state: TracebackState) -> str:
    """审核③后: approve → report; revise → attribute(重算归因)"""
    decision = state.get("review_decision") or {}
    return "report" if decision.get("action") == "approve" else "attribute"


def route_after_review_knowledge(state: TracebackState) -> str:
    """审核④后: approve → END; revise → knowledge_sink(重新沉淀)"""
    decision = state.get("review_decision") or {}
    return END_NODE if decision.get("action") == "approve" else "knowledge_sink"


# 默认业务节点注册表 (审核 interrupt 节点固定, 不可覆盖)
DEFAULT_NODES: dict[str, NodeFn] = {
    "collect_evidence": collect_evidence_node,
    "build_timeline": build_timeline_node,
    "analyze_chain": analyze_chain_node,
    "attribute": attribute_node,
    "report": report_node,
    "knowledge_sink": knowledge_sink_node,
}
