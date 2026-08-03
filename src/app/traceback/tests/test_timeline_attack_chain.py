"""S3 时间线 + 攻击链单元测试

对齐 01_phase1_design.md §4.2 / §4.3 / §11
验证: ATT&CK 白名单校验、TimelineBuilder 规则提取+关键节点、AttackChainAgent 入口/横向/TTP/路径图。
不依赖外部服务 (DB/Mongo/LLM 均注入或降级)。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from src.app.traceback.services.mitre import (
    is_valid_technique,
    load_whitelist,
    tactic_of,
    validate_ttp,
)


# ---------- ATT&CK 白名单 ----------

def test_whitelist_loaded():
    data = load_whitelist()
    assert "T1566" in data["techniques"]
    assert len(data["tactics"]) >= 11


def test_is_valid_technique_includes_subtechniques():
    assert is_valid_technique("T1566") is True       # 父技术
    assert is_valid_technique("T1566.001") is True   # 子技术(父命中)
    assert is_valid_technique("T9999") is False      # 不在白名单
    assert is_valid_technique(None) is False


def test_tactic_of():
    assert tactic_of("T1486") == "impact"
    assert tactic_of("T1021.001") == "lateral_movement"
    assert tactic_of("T9999") is None


def test_validate_ttp_demotes_unknown():
    """未命中白名单的 TTP → technique_id=None, tactic=unknown, 置信度减半"""
    tid, tactic, conf = validate_ttp("T1566.001", 0.9)
    assert tid == "T1566.001"
    assert tactic == "initial_access"
    assert conf == 0.9

    tid, tactic, conf = validate_ttp("T9999", 0.9)
    assert tid is None
    assert tactic == "unknown"
    assert conf == 0.45  # 减半


# ---------- TimelineBuilder (规则兜底, 注入会话/repo) ----------

def _mock_evidence(task_id: str = "t-tl") -> list[dict[str, Any]]:
    """模拟已入库的 mock 证据(parsed_data 含 event/ts)"""
    return [
        {"evidence_id": "evd-1", "task_id": task_id, "evidence_type": "log", "source": "siem",
         "parsed_data": {"event": "phishing_email_received", "ts": "2026-07-01T08:05:00Z",
                         "src": "external", "dst": "10.0.0.5"}, "raw_summary": None},
        {"evidence_id": "evd-2", "task_id": task_id, "evidence_type": "log", "source": "siem",
         "parsed_data": {"event": "powershell_execution", "ts": "2026-07-01T08:12:00Z",
                         "src": "10.0.0.5", "dst": "203.0.113.55"}, "raw_summary": None},
        {"evidence_id": "evd-3", "task_id": task_id, "evidence_type": "log", "source": "siem",
         "parsed_data": {"event": "c2_beacon", "ts": "2026-07-01T08:30:00Z",
                         "src": "10.0.0.5", "dst": "203.0.113.55"}, "raw_summary": None},
        {"evidence_id": "evd-4", "task_id": task_id, "evidence_type": "log", "source": "siem",
         "parsed_data": {"event": "rdp_lateral", "ts": "2026-07-01T09:10:00Z",
                         "src": "10.0.0.5", "dst": "10.0.0.6"}, "raw_summary": None},
        {"evidence_id": "evd-5", "task_id": task_id, "evidence_type": "log", "source": "siem",
         "parsed_data": {"event": "file_encryption", "ts": "2026-07-01T09:45:00Z",
                         "src": "10.0.0.5"}, "raw_summary": None},
    ]


@pytest.mark.asyncio
async def test_timeline_builder_rule_based(monkeypatch):
    """规则兜底: 从 mock 证据提取 5 类事件, 标记关键节点, 按时序排序"""
    from src.app.traceback.agents import timeline_builder as tb_mod
    from src.app.traceback.agents.timeline_builder import TimelineBuilder

    saved: dict[str, Any] = {}

    async def fake_list(session, task_id, **kw):
        return [{"evidence_id": e["evidence_id"]} for e in _mock_evidence(task_id)]

    async def fake_get(session, eid):
        return next(e for e in _mock_evidence() if e["evidence_id"] == eid)

    async def fake_save(task_id, events):
        saved[task_id] = events
        return len(events)

    @asynccontextmanager
    async def fake_session():
        yield None

    monkeypatch.setattr(tb_mod.evidence_repo, "list_evidence", fake_list)
    monkeypatch.setattr(tb_mod.evidence_repo, "get_evidence", fake_get)
    monkeypatch.setattr(tb_mod.timeline_repo, "save_events", fake_save)
    monkeypatch.setattr(tb_mod, "session_scope", fake_session)

    agent = TimelineBuilder()
    result = await agent.run({"task_id": "t-tl", "stage": "collecting"})

    events = result["timeline_events"]
    assert result["stage"] == "timeline_built"
    assert len(events) == 5
    # 按时间升序
    times = [e["event_time"] for e in events]
    assert times == sorted(times)
    # 关键节点: initial_access/lateral_movement/impact 标记
    categories = {e["event_category"] for e in events}
    assert "initial_access" in categories
    assert "lateral_movement" in categories
    assert "impact" in categories
    key_events = [e for e in events if e["is_key_event"]]
    assert len(key_events) >= 3
    # TTP 标注
    assert any("T1566.001" in e["attack_techniques"] for e in events)
    assert any("T1486" in e["attack_techniques"] for e in events)
    # 已写入 Mongo
    assert "t-tl" in saved and len(saved["t-tl"]) == 5


def test_timeline_rule_based_dedup():
    """重复事件(同时间+标题)应去重"""
    from src.app.traceback.agents.timeline_builder import TimelineBuilder
    agent = TimelineBuilder.__new__(TimelineBuilder)
    ev = _mock_evidence()[0]
    events = agent._extract_rule_based([ev, ev])
    deduped = agent._dedup_sort(events)
    assert len(deduped) == 1


# ---------- AttackChainAgent (纯函数, 用 state 时间线) ----------

def _timeline_events() -> list[dict[str, Any]]:
    """从 TimelineBuilder 规则提取得到的典型时间线"""
    from src.app.traceback.agents.timeline_builder import TimelineBuilder
    agent = TimelineBuilder.__new__(TimelineBuilder)
    events = agent._extract_rule_based(_mock_evidence())
    agent._mark_key_events(events)
    return events


def test_attack_chain_entry_and_lateral():
    """入口=initial_access, 横向移动 from 10.0.0.5 → 10.0.0.6"""
    from src.app.traceback.agents.attack_chain import AttackChainAgent

    agent = AttackChainAgent.__new__(AttackChainAgent)
    chain = agent._build_chain("t-ac", _timeline_events())

    assert chain["entry_point"] is not None
    assert chain["entry_point"]["asset"] == "10.0.0.5"
    assert chain["entry_point"]["technique_id"] == "T1566.001"
    # 横向移动
    assert len(chain["lateral_movements"]) == 1
    lm = chain["lateral_movements"][0]
    assert lm["from_asset"] == "10.0.0.5"
    assert lm["to_asset"] == "10.0.0.6"
    assert lm["technique_id"] == "T1021.001"
    # 受影响资产
    assert "10.0.0.5" in chain["affected_assets"]
    assert "10.0.0.6" in chain["affected_assets"]
    # TTP 汇总(白名单校验后, 非法 ID 被 None 化)
    techs = {t["technique_id"] for t in chain["ttp_summary"]}
    assert "T1566.001" in techs
    assert "T1486" in techs


def test_attack_chain_path_graph():
    """路径图: 节点含受害资产, 边含入口+横向"""
    from src.app.traceback.agents.attack_chain import AttackChainAgent

    agent = AttackChainAgent.__new__(AttackChainAgent)
    chain = agent._build_chain("t-ac", _timeline_events())
    g = chain["path_graph"]
    node_ids = {n["id"] for n in g["nodes"]}
    assert "10.0.0.5" in node_ids
    assert "10.0.0.6" in node_ids
    # 至少一条横向边 10.0.0.5 → 10.0.0.6
    lateral_edges = [e for e in g["edges"] if e["from"] == "10.0.0.5" and e["to"] == "10.0.0.6"]
    assert len(lateral_edges) == 1
    assert lateral_edges[0]["technique"] == "T1021.001"


def test_attack_chain_external_ip_extraction():
    """C2 外部 IP 应识别为攻击者基础设施"""
    from src.app.traceback.agents.attack_chain import AttackChainAgent, _is_external_ip

    assert _is_external_ip("203.0.113.55") is True
    assert _is_external_ip("10.0.0.5") is False
    assert _is_external_ip("192.168.1.1") is False

    agent = AttackChainAgent.__new__(AttackChainAgent)
    chain = agent._build_chain("t-ac", _timeline_events())
    assert "203.0.113.55" in chain["attacker_ips"]


@pytest.mark.asyncio
async def test_attack_chain_agent_persists(monkeypatch):
    """智能体 run 应持久化攻击链到 PG"""
    from src.app.traceback.agents import attack_chain as ac_mod
    from src.app.traceback.agents.attack_chain import AttackChainAgent

    saved: dict[str, dict] = {}

    async def fake_save(session, task_id, payload):
        saved[task_id] = payload
        return "chain-id"

    @asynccontextmanager
    async def fake_session():
        yield None

    monkeypatch.setattr(ac_mod.attack_chain_repo, "save_chain", fake_save)
    monkeypatch.setattr(ac_mod, "session_scope", fake_session)

    agent = AttackChainAgent()
    result = await agent.run({
        "task_id": "t-ac", "stage": "review_timeline",
        "timeline_events": _timeline_events(),
    })
    assert result["stage"] == "chain_analyzed"
    assert result["attack_chain"]["entry_point"]["asset"] == "10.0.0.5"
    assert "t-ac" in saved  # 已持久化
