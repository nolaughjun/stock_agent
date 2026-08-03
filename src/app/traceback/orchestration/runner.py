"""编排运行器: 驱动 LangGraph 图的启动与中断-恢复

对齐 01_phase1_design.md §3.4 / §9 时序

P1 检查点策略:
  - 业务状态(stage/审核决策)同步写入 PG traceback_tasks, 进度可查询、进程重启可恢复
  - LangGraph checkpointer 用 MemorySaver(M1); 生产级 PG checkpointer 见 §5.1 (S4 增强)
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from langgraph.types import Command

from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.graph import get_graph
from src.app.traceback.orchestration.state import STAGE_PROGRESS, AWAITING_REVIEW_STAGES

# 中断节点 → 有效审核阶段 (interrupt 暂停时该节点尚未写 stage, 需从 next 推导)
_INTERRUPT_STAGE: dict[str, str] = {
    "review_timeline": "review_timeline",
    "review_chain": "review_chain",
    "review_attribution": "review_attribution",
}


def _config(task_id: str) -> dict[str, Any]:
    """LangGraph 线程配置: thread_id = task_id"""
    return {"configurable": {"thread_id": task_id}}


def _effective_stage(snapshot: Any) -> str:
    """从快照推导有效阶段。

    interrupt 暂停在审核节点时, 该节点尚未返回 stage, snapshot.values['stage']
    仍是上一阶段(timeline_built/chain_analyzed)。此时须从 snapshot.next 推导出
    review_timeline/review_chain, 否则进度接口误判、/review 校验失败。
    """
    values = snapshot.values or {}
    if snapshot.next:  # 被中断
        for node in snapshot.next:
            if node in _INTERRUPT_STAGE:
                return _INTERRUPT_STAGE[node]
    return values.get("stage", "created")


async def run_until_interrupt(
    task_id: str,
    initial_state: Optional[dict[str, Any]] = None,
    graph: Any = None,
) -> dict[str, Any]:
    """启动/继续运行图, 直到遇到 interrupt(审核节点) 或 END。

    Args:
        graph: 可选, 测试注入编译图; 默认用全局 get_graph()
    Returns:
        最终 state 快照
    """
    graph = graph or get_graph()
    config = _config(task_id)

    if initial_state is not None:
        # 首次启动: 传入初始状态
        stream: AsyncIterator = graph.astream(initial_state, config=config)
    else:
        # 已有 checkpoint, 无需新输入
        stream = graph.astream(None, config=config)

    async for chunk in stream:
        logger.debug("[{}] 流式 chunk keys={}", task_id, list(chunk.keys()))

    # 取完整快照(含累积 reducer 结果)
    snapshot = await graph.aget_state(config)
    state_values = dict(snapshot.values or {})
    # 修正: 中断时从 next 推导有效审核阶段
    state_values["stage"] = _effective_stage(snapshot)
    stage = state_values["stage"]
    logger.info(
        "[{}] 运行暂停/完成 stage={} awaiting_review={}",
        task_id, stage, stage in AWAITING_REVIEW_STAGES,
    )
    return state_values


async def resume_with_decision(
    task_id: str, decision: dict[str, Any], graph: Any = None
) -> dict[str, Any]:
    """人工审核后, 携带决策恢复运行 (POST /resume)。

    decision: {action, comments, modifications, reviewer}
    """
    graph = graph or get_graph()
    config = _config(task_id)
    stream = graph.astream(Command(resume=decision), config=config)
    async for _ in stream:
        pass
    snapshot = await graph.aget_state(config)
    state_values = dict(snapshot.values or {})
    state_values["stage"] = _effective_stage(snapshot)
    return state_values


def progress_of(state: dict[str, Any]) -> dict[str, Any]:
    """从状态计算 TaskProgress 字段"""
    stage = state.get("stage", "created")
    return {
        "current_stage": stage,
        "progress_percent": STAGE_PROGRESS.get(stage, 0),
        "awaiting_review": stage in AWAITING_REVIEW_STAGES,
        "review_stage": _review_stage_of(stage),
    }


def _review_stage_of(stage: str) -> Optional[str]:
    mapping = {"review_timeline": "timeline", "review_chain": "attack_chain"}
    return mapping.get(stage)
