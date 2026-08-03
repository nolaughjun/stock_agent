"""S4 报告生成单元测试 + 迷你 UAT 管道

对齐 01_phase1_design.md §4.4 / §11 / UAT-001
验证: 路径图 SVG、Reporter 规则叙事+模板渲染、证据→时间线→攻击链→报告 端到端管道。
PDF 转换依赖 weasyprint 原生库(生产 Linux 可用, 本机 Windows 可能缺失 → 优雅降级仅存 MD)。
不依赖外部服务 (MinIO/LLM/DB 均注入或降级)。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest


# ---------- 路径图 SVG ----------

def test_path_graph_svg_renders_nodes_and_edges():
    from src.app.traceback.services.path_graph import render_path_graph_svg

    svg = render_path_graph_svg({
        "nodes": [
            {"id": "203.0.113.55", "type": "attacker"},
            {"id": "10.0.0.5", "type": "victim"},
            {"id": "10.0.0.6", "type": "victim"},
        ],
        "edges": [
            {"from": "203.0.113.55", "to": "10.0.0.5", "technique": "T1566.001", "label": "钓鱼"},
            {"from": "10.0.0.5", "to": "10.0.0.6", "technique": "T1021.001", "label": "RDP"},
        ],
    })
    assert svg.startswith("<svg")
    assert "10.0.0.5" in svg
    assert "10.0.0.6" in svg
    assert "arrow" in svg  # 有边箭头


def test_path_graph_empty():
    from src.app.traceback.services.path_graph import render_path_graph_svg

    svg = render_path_graph_svg({"nodes": [], "edges": []})
    assert "无攻击路径数据" in svg


# ---------- Reporter 规则叙事 ----------

def test_reporter_rule_narrative():
    from src.app.traceback.agents.reporter import Reporter

    reporter = Reporter.__new__(Reporter)
    narrative = reporter._rule_narrative(
        task={"task_name": "勒索病毒事件"},
        timeline=[{"is_key_event": True}, {"is_key_event": False}],
        attack_chain={
            "entry_point": {"asset": "10.0.0.5", "vector": "phishing", "technique_id": "T1566.001"},
            "affected_assets": ["10.0.0.5"], "attacker_ips": ["203.0.113.55"],
        },
        attribution=None,
    )
    assert "勒索病毒事件" in narrative.overview
    assert "10.0.0.5" in narrative.attack_narrative
    assert "203.0.113.55" in narrative.response_suggestions  # 封禁 IP


# ---------- Reporter 渲染报告 (注入 MinIO 存储) ----------

@pytest.mark.asyncio
async def test_reporter_renders_report(monkeypatch):
    from src.app.traceback.agents import reporter as rep_mod
    from src.app.traceback.agents.reporter import Reporter
    from src.app.traceback.services import report_service

    stored: dict[str, bytes] = {}

    async def fake_upload(task_id, content, fmt):
        stored[fmt] = content
        return f"minio:{task_id}/report.{fmt}"

    monkeypatch.setattr(rep_mod.report_service, "upload_report_to_minio", fake_upload)
    # PDF 转换在本机可能失败, 强制走 MD
    monkeypatch.setattr(rep_mod.report_service, "markdown_to_pdf", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no gtk")))

    timeline = [
        {"event_time": "2026-07-01T08:05:00", "title": "钓鱼邮件", "event_category": "initial_access",
         "severity": "high", "is_key_event": True, "attack_techniques": ["T1566.001"]},
        {"event_time": "2026-07-01T09:45:00", "title": "文件加密", "event_category": "impact",
         "severity": "critical", "is_key_event": True, "attack_techniques": ["T1486"]},
    ]
    attack_chain = {
        "entry_point": {"asset": "10.0.0.5", "vector": "phishing", "technique_id": "T1566.001"},
        "lateral_movements": [{"from_asset": "10.0.0.5", "to_asset": "10.0.0.6",
                                "technique_id": "T1021.001", "technique_label": "RDP"}],
        "affected_assets": ["10.0.0.5", "10.0.0.6"], "data_exfiltration": False,
        "attacker_ips": ["203.0.113.55"], "attacker_domains": [], "attacker_tools": ["PowerShell"],
        "ttp_summary": [{"technique_id": "T1566.001", "tactic": "initial_access", "confidence": 0.8, "event_ids": ["e1"], "raw_id": "T1566.001"}],
        "path_graph": {"nodes": [{"id": "10.0.0.5", "type": "victim"}], "edges": []},
    }

    reporter = Reporter()
    result = await reporter.run({
        "task_id": "t-rpt", "stage": "attributed",
        "task": {"task_id": "t-rpt", "task_name": "勒索病毒溯源"},
        "timeline_events": timeline, "attack_chain": attack_chain,
        "attribution": {"attributed": False, "confidence_score": 0.0},
    })

    assert result["stage"] == "reported"
    formats = result["report_formats"]
    assert "md" in formats
    md = stored["md"].decode("utf-8")
    # 结构化数据渲染
    assert "勒索病毒溯源" in md
    assert "钓鱼邮件" in md and "文件加密" in md
    assert "10.0.0.5" in md and "10.0.0.6" in md
    assert "T1566.001" in md
    # 路径图 SVG 嵌入
    assert "<svg" in md
    # TTP 矩阵
    assert "TTP 矩阵" in md


# ---------- 迷你 UAT: 证据 → 时间线 → 攻击链 → 报告 ----------

def _mock_evidence(task_id: str = "t-uat") -> list[dict[str, Any]]:
    return [
        {"evidence_id": f"evd-{i}", "task_id": task_id, "evidence_type": "log", "source": "siem",
         "parsed_data": p, "raw_summary": None}
        for i, p in enumerate([
            {"event": "phishing_email_received", "ts": "2026-07-01T08:05:00Z", "src": "external", "dst": "10.0.0.5"},
            {"event": "powershell_execution", "ts": "2026-07-01T08:12:00Z", "src": "10.0.0.5", "dst": "203.0.113.55"},
            {"event": "c2_beacon", "ts": "2026-07-01T08:30:00Z", "src": "10.0.0.5", "dst": "203.0.113.55"},
            {"event": "rdp_lateral", "ts": "2026-07-01T09:10:00Z", "src": "10.0.0.5", "dst": "10.0.0.6"},
            {"event": "file_encryption", "ts": "2026-07-01T09:45:00Z", "src": "10.0.0.5"},
        ])
    ]


@pytest.mark.asyncio
async def test_mini_uat_pipeline(monkeypatch):
    """UAT-001 简化版: 端到端跑通 证据→时间线→攻击链→报告, 断言报告完整度。

    全程规则兜底(无 LLM), 存储注入, 不依赖外部服务。
    """
    from src.app.traceback.agents import reporter as rep_mod
    from src.app.traceback.agents import timeline_builder as tb_mod
    from src.app.traceback.agents.attack_chain import AttackChainAgent
    from src.app.traceback.agents.reporter import Reporter
    from src.app.traceback.agents.timeline_builder import TimelineBuilder

    @asynccontextmanager
    async def fake_session():
        yield None

    # timeline builder: 注入证据读取 + 存储
    async def fake_list(session, task_id, **kw):
        return [{"evidence_id": e["evidence_id"]} for e in _mock_evidence(task_id)]

    async def fake_get(session, eid):
        return next(e for e in _mock_evidence() if e["evidence_id"] == eid)

    saved_timeline: dict[str, list] = {}

    async def fake_save_timeline(task_id, events):
        saved_timeline[task_id] = events
        return len(events)

    monkeypatch.setattr(tb_mod.evidence_repo, "list_evidence", fake_list)
    monkeypatch.setattr(tb_mod.evidence_repo, "get_evidence", fake_get)
    monkeypatch.setattr(tb_mod.timeline_repo, "save_events", fake_save_timeline)
    monkeypatch.setattr(tb_mod, "session_scope", fake_session)

    # attack chain: 注入持久化
    saved_chain: dict[str, dict] = {}

    async def fake_save_chain(session, task_id, payload):
        saved_chain[task_id] = payload
        return "chain-id"

    monkeypatch.setattr("src.app.traceback.agents.attack_chain.attack_chain_repo.save_chain", fake_save_chain)
    monkeypatch.setattr("src.app.traceback.agents.attack_chain.session_scope", fake_session)

    # reporter: 注入存储
    stored_reports: dict[str, bytes] = {}

    async def fake_upload(task_id, content, fmt):
        stored_reports[fmt] = content
        return f"minio:{task_id}/report.{fmt}"

    monkeypatch.setattr(rep_mod.report_service, "upload_report_to_minio", fake_upload)
    monkeypatch.setattr(rep_mod.report_service, "markdown_to_pdf", lambda *a, **k: None)

    # 1. 时间线
    tb = TimelineBuilder()
    tl_result = await tb.run({"task_id": "t-uat", "stage": "collecting"})
    events = tl_result["timeline_events"]
    assert len(events) == 5
    assert any(e["event_category"] == "initial_access" for e in events)
    assert any(e["event_category"] == "impact" for e in events)

    # 2. 攻击链
    ac = AttackChainAgent()
    ac_result = await ac.run({
        "task_id": "t-uat", "stage": "review_timeline", "timeline_events": events,
    })
    chain = ac_result["attack_chain"]
    assert chain["entry_point"]["asset"] == "10.0.0.5"
    assert any(lm["to_asset"] == "10.0.0.6" for lm in chain["lateral_movements"])

    # 3. 报告
    reporter = Reporter()
    rpt_result = await reporter.run({
        "task_id": "t-uat", "stage": "attributed",
        "task": {"task_id": "t-uat", "task_name": "UAT-001 勒索病毒溯源"},
        "timeline_events": events, "attack_chain": chain,
        "attribution": {"attributed": False, "confidence_score": 0.0},
    })
    assert rpt_result["stage"] == "reported"
    md = stored_reports["md"].decode("utf-8")
    # 报告完整度断言 (UAT-001): 事件概述/时间线/攻击链/处置建议 齐全
    assert "UAT-001 勒索病毒溯源" in md
    assert "攻击时间线" in md
    assert "攻击链分析" in md
    assert "处置建议" in md
    assert "T1566.001" in md and "T1486" in md
    assert "<svg" in md  # 路径图
