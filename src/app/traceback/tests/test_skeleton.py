"""M1 骨架单元测试

对齐 01_phase1_design.md §11 (单测覆盖率>80%)
验证:
  - schemas 校验
  - 适配器基座/manager 注册+降级
  - LangGraph 编排: 启动→审核①interrupt→恢复→审核②interrupt→恢复→END
  - 报告模板渲染
不依赖外部服务 (PG/Mongo/MinIO/Redis/LLM)。
"""
from __future__ import annotations

import pytest

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.adapters.manager import ComponentManager
from src.app.traceback.orchestration.graph import build_graph
from src.app.traceback.orchestration.nodes import (
    analyze_chain_stub,
    build_timeline_stub,
    collect_evidence_stub,
)
from src.app.traceback.orchestration.state import STAGE_PROGRESS
from src.app.traceback.schemas.attack_chain import AttackChain
from src.app.traceback.schemas.evidence import EvidenceMeta
from src.app.traceback.schemas.task import TaskCreate
from src.app.traceback.schemas.timeline import TimelineEvent

# 业务节点用 stub, 避免触达 DB/适配器; 审核 interrupt 节点为真实实现(被测对象)
_STUB_NODES = {
    "collect_evidence": collect_evidence_stub,
    "build_timeline": build_timeline_stub,
    "analyze_chain": analyze_chain_stub,
}


# ---------- schemas ----------

def test_task_create_validation():
    t = TaskCreate(task_name="勒索病毒事件", asset_ips=["10.0.0.1"])
    assert t.task_name == "勒索病毒事件"
    assert t.asset_ips == ["10.0.0.1"]
    with pytest.raises(Exception):
        TaskCreate(task_name="")


def test_evidence_meta_defaults():
    from datetime import datetime
    e = EvidenceMeta(
        evidence_id="e1", task_id="t1", evidence_type="log",
        source="siem", collect_time=datetime.now(),
    )
    assert e.importance == "medium"
    assert e.unavailable is False


def test_attack_chain_path_graph_default():
    c = AttackChain(chain_id="c1", task_id="t1")
    assert c.path_graph == {"nodes": [], "edges": []}


def test_timeline_event_category_enum():
    from datetime import datetime
    ev = TimelineEvent(
        event_id="ev1", task_id="t1", event_time=datetime.now(),
        event_type="login", event_category="initial_access", title="登录",
    )
    assert ev.event_category == "initial_access"


# ---------- adapters ----------

class _FailAdapter(BaseAdapter):
    name = "fail"

    async def connect(self) -> bool:
        return False

    async def health_check(self) -> bool:
        return False

    async def close(self) -> None:
        pass


class _OkAdapter(BaseAdapter):
    name = "ok"

    async def connect(self) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_manager_degrades_on_failure():
    """单个适配器失败不影响其他 (§7.3 降级)"""
    m = ComponentManager()
    m.register("fail", _FailAdapter())
    m.register("ok", _OkAdapter())
    result = await m.initialize()
    assert result["fail"] is False
    assert result["ok"] is True
    assert m.has("ok") is True
    assert m.has("fail") is False
    await m.close_all()


# ---------- orchestration: interrupt/resume ----------

@pytest.mark.asyncio
async def test_graph_interrupt_and_resume():
    """核心: 启动→审核①interrupt→approve恢复→审核②interrupt→approve恢复→END"""
    graph = build_graph(node_overrides=_STUB_NODES)
    config = {"configurable": {"thread_id": "t-test"}}

    # 1. 启动, 应在 review_timeline 处 interrupt
    state = await graph.ainvoke(
        {"task_id": "t-test", "stage": "created"}, config=config
    )
    snapshot = await graph.aget_state(config)
    # interrupt 后 next 应是 review_timeline
    assert snapshot.next, "应在审核①处暂停"
    assert "review_timeline" in snapshot.next

    # 2. 恢复审核① approve
    from langgraph.types import Command
    await graph.ainvoke(
        Command(resume={"action": "approve", "reviewer": "tester"}), config=config
    )
    snapshot = await graph.aget_state(config)
    stage_after = snapshot.values.get("stage")
    # approve 后进入 chain_analyzed → review_chain interrupt
    assert stage_after in ("chain_analyzed", "review_chain")
    assert snapshot.next, "应在审核②处暂停"
    assert "review_chain" in snapshot.next

    # 3. 恢复审核② approve → END
    await graph.ainvoke(
        Command(resume={"action": "approve", "reviewer": "tester"}), config=config
    )
    snapshot = await graph.aget_state(config)
    assert snapshot.next == (), "应已到达 END"
    assert snapshot.values.get("stage") == "reported"
    # 归因桩置信度 0
    assert snapshot.values.get("attribution", {}).get("confidence_score") == 0.0


@pytest.mark.asyncio
async def test_graph_revise_loops_back():
    """revise 审核①应回退到 collect_evidence"""
    graph = build_graph(node_overrides=_STUB_NODES)
    config = {"configurable": {"thread_id": "t-revise"}}
    await graph.ainvoke({"task_id": "t-revise", "stage": "created"}, config=config)
    from langgraph.types import Command
    await graph.ainvoke(
        Command(resume={"action": "revise", "reviewer": "tester"}), config=config
    )
    snapshot = await graph.aget_state(config)
    # revise 后回退 collect_evidence → build_timeline → 再次停在 review_timeline
    assert snapshot.next and "review_timeline" in snapshot.next


def test_stage_progress_monotonic():
    """进度百分比随阶段递增"""
    order = ["created", "collecting", "timeline_built", "review_timeline",
             "chain_analyzed", "review_chain", "attributed", "reported"]
    vals = [STAGE_PROGRESS[s] for s in order]
    assert vals == sorted(vals)
    assert STAGE_PROGRESS["reported"] == 100


@pytest.mark.asyncio
async def test_runner_effective_stage_after_interrupt():
    """回归: 中断后 runner 返回的有效阶段应为 review_timeline, 而非 timeline_built。

    interrupt 暂停时审核节点尚未写 stage, 须从 snapshot.next 推导,
    否则进度接口误判、/review 校验失败。
    """
    from src.app.traceback.orchestration import runner
    graph = build_graph(node_overrides=_STUB_NODES)
    # 每个测试用独立 thread_id 避免与其它用例 checkpoint 冲突
    state = await runner.run_until_interrupt(
        "t-runner-stage",
        initial_state={"task_id": "t-runner-stage", "stage": "created"},
        graph=graph,
    )
    assert state["stage"] == "review_timeline", (
        f"中断后有效阶段应为 review_timeline, 实际 {state['stage']}"
    )

    # 恢复 approve → 应停在 review_chain
    state = await runner.resume_with_decision(
        "t-runner-stage", {"action": "approve", "reviewer": "tester"}, graph=graph
    )
    assert state["stage"] == "review_chain", (
        f"审核①approve 后应停在 review_chain, 实际 {state['stage']}"
    )

    # 恢复 approve → END, stage=reported
    state = await runner.resume_with_decision(
        "t-runner-stage", {"action": "approve", "reviewer": "tester"}, graph=graph
    )
    assert state["stage"] == "reported"


# ---------- report template ----------

@pytest.mark.asyncio
async def test_report_template_renders():
    from datetime import datetime
    from src.app.traceback.services.report_service import render_markdown

    md = await render_markdown(
        "report_detail.md.j2",
        task={"task_id": "t1", "task_name": "测试事件"},
        timeline_events=[{
            "event_time": datetime.now(), "title": "异常登录",
            "event_category": "initial_access", "severity": "high", "is_key_event": True,
        }],
        overview="测试概述",
    )
    assert "测试事件" in md
    assert "异常登录" in md
    assert "关键节点数**: 1" in md
