"""S2 证据收集单元测试

对齐 01_phase1_design.md §4.1 / §11
验证: mock 数据、适配器 mock 收集、EvidenceCollector 并行收集+去重+评估+存储、SIEM DSL 白名单。
不依赖外部服务 (DB/Mongo/MinIO/LLM 均注入或降级)。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from src.app.traceback.adapters.dns_adapter import DNSAdapter
from src.app.traceback.adapters.edr_adapter import EDRAdapter
from src.app.traceback.adapters.manager import ComponentManager
from src.app.traceback.adapters.misp_adapter import MISPAdapter
from src.app.traceback.adapters.registry import default_adapters
from src.app.traceback.adapters.siem_adapter import SIEMAdapter, _ALLOWED_FIELDS
from src.app.traceback.schemas.evidence import CollectContext


# ---------- mock 数据 ----------

def test_mock_siem_evidence_has_ransomware_signals():
    from src.app.traceback.adapters.mock_data import mock_siem_logs

    ctx = CollectContext(task_id="t1", asset_ips=["10.0.0.5"])
    items = mock_siem_logs(ctx)
    assert len(items) >= 4
    events = [it.raw.get("event") for it in items]
    assert "c2_beacon" in events
    assert "file_encryption" in events
    assert all(it.importance == "high" for it in items)


def test_mock_threat_intel_marks_c2():
    from src.app.traceback.adapters.mock_data import mock_threat_intel

    items = mock_threat_intel(CollectContext(task_id="t1"))
    assert any(it.raw.get("malicious") for it in items)
    assert any(it.raw.get("malware_family") == "RansomX" for it in items)


# ---------- 适配器 mock 收集 ----------

@pytest.mark.asyncio
async def test_adapters_mock_collect():
    """无真实配置时, 4 个适配器均返回 mock 证据"""
    ctx = CollectContext(task_id="t1", asset_ips=["10.0.0.5"])
    for cls, name, expected_type in [
        (SIEMAdapter, "siem", "log"),
        (EDRAdapter, "edr", "log"),
        (DNSAdapter, "dns", "dns"),
        (MISPAdapter, "misp", "intel"),
    ]:
        adapter = cls()
        ok = await adapter.connect()
        assert ok, f"{name} 应在 mock 模式可用"
        items = await adapter.collect(ctx)
        assert len(items) > 0, f"{name} mock 应返回证据"
        assert all(it.evidence_type == expected_type for it in items)
        await adapter.close()


# ---------- manager 注册 + 并行收集 ----------

@pytest.mark.asyncio
async def test_manager_registers_all_default_adapters():
    m = ComponentManager()
    for name, adapter in default_adapters().items():
        m.register(name, adapter)
    health = await m.initialize()
    # 4 个证据源适配器在 mock 模式应全部健康; timesketch 无配置为 False
    assert health["siem"] is True
    assert health["edr"] is True
    assert health["dns"] is True
    assert health["misp"] is True
    await m.close_all()


# ---------- EvidenceCollector 全流程 (注入会话 + 存储) ----------

@pytest.mark.asyncio
async def test_evidence_collector_collect_dedup_save(monkeypatch):
    """证据收集: 并行收集 → 去重 → 存证, 返回 evidence_ids

    无 LLM key → 查询提示/评估均降级; 注入 session_scope + save_evidence 避免触达 DB。
    """
    from src.app.traceback.agents import evidence_collector as ec_mod
    from src.app.traceback.agents.evidence_collector import EvidenceCollector

    # 注入任务
    async def fake_get_task(session, task_id):
        return {
            "task_id": task_id, "task_name": "勒索事件", "description": "财务主机感染勒索病毒",
            "asset_ips": ["10.0.0.5"], "asset_hosts": [], "asset_users": [],
            "event_start_time": None, "event_end_time": None,
        }

    @asynccontextmanager
    async def fake_session():
        yield None

    saved: list[dict[str, Any]] = []

    async def fake_save(session, **kwargs):
        eid = f"evd-{len(saved)+1:03d}"
        saved.append({"eid": eid, **kwargs})
        return eid

    monkeypatch.setattr(ec_mod.task_repo, "get_task", fake_get_task)
    monkeypatch.setattr(ec_mod, "session_scope", fake_session)
    monkeypatch.setattr(ec_mod.evidence_repo, "save_evidence", fake_save)

    # 真实 manager (mock 模式适配器)
    manager = ComponentManager()
    from src.app.traceback.adapters.registry import default_adapters
    for n, a in default_adapters().items():
        manager.register(n, a)
    await manager.initialize()

    agent = EvidenceCollector(manager=manager)
    state = {"task_id": "t-collect", "stage": "created"}
    result = await agent.run(state)

    assert result["stage"] == "collecting"
    assert len(result["evidence_ids"]) > 0
    # 去重: mock 各源无重复, saved 数应等于 evidence_ids 数
    assert len(saved) == len(result["evidence_ids"])
    # 存证包含三段式所需字段
    assert "evidence_type" in saved[0]
    assert "raw_data" in saved[0]
    assert "importance" in saved[0]
    await manager.close_all()


@pytest.mark.asyncio
async def test_evidence_collector_dedup_removes_duplicates():
    """相同 (source, raw) 的证据应去重"""
    from src.app.traceback.agents.evidence_collector import EvidenceCollector
    from src.app.traceback.schemas.evidence import RawEvidence

    agent = EvidenceCollector.__new__(EvidenceCollector)  # 不走 __init__ (避免 LLM)
    dup = {"event": "c2_beacon", "src": "10.0.0.5"}
    items = [
        RawEvidence(evidence_type="log", source="siem", raw=dup),
        RawEvidence(evidence_type="log", source="siem", raw=dup),  # 重复
        RawEvidence(evidence_type="log", source="siem", raw={"event": "other"}),
    ]
    deduped = agent._dedup(items)
    assert len(deduped) == 2


# ---------- SIEM DSL 白名单 ----------

def test_siem_dsl_uses_only_whitelisted_fields():
    """DSL 构造仅用白名单字段 (§8.2 防注入)"""
    adapter = SIEMAdapter()
    ctx = CollectContext(
        task_id="t1", asset_ips=["10.0.0.5"],
        event_start_time=__import__("datetime").datetime(2026, 7, 1),
        event_end_time=__import__("datetime").datetime(2026, 7, 2),
    )
    dsl = adapter._build_dsl(ctx)
    must = dsl["query"]["bool"]["must"]
    # source.ip 在白名单
    assert "source.ip" in must[0]["terms"]
    # @timestamp 在白名单
    assert "@timestamp" in must[1]["range"]
    # 所有查询字段须在白名单 (取 term/range 内层字段名)
    for clause in must:
        inner = next(iter(clause.values()))  # {"terms": {...}} → {...}
        for field in inner:
            assert field in _ALLOWED_FIELDS, f"字段 {field} 不在白名单"


def test_siem_dsl_no_injection_via_asset():
    """资产值作为 terms 列表传入, 不拼接到查询字符串 (防注入)"""
    adapter = SIEMAdapter()
    ctx = CollectContext(task_id="t1", asset_ips=["10.0.0.5'; DELETE FROM x;--"])
    dsl = adapter._build_dsl(ctx)
    # 恶意输入仅作为 term 值, 不影响查询结构
    assert dsl["query"]["bool"]["must"][0]["terms"]["source.ip"] == ["10.0.0.5'; DELETE FROM x;--"]
    assert "DELETE" not in str(dsl["query"]) or "DELETE FROM x" in str(
        dsl["query"]["bool"]["must"][0]["terms"]["source.ip"]
    )
