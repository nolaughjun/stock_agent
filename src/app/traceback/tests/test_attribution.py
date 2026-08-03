"""P2 归因智能体 + 深度取证测试

对齐 02_phase2_design.md §3 / §4 / §9.2 / §11
验证: 三因子归因算法、相似案例匹配、归因黄金样本 Top1>70%、深度适配器 mock 收集、
OpenCTI 种子降级、归因审核③路由。
不依赖外部服务 (DB/LLM/OpenCTI 均注入或降级)。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.app.traceback.adapters.manager import ComponentManager
from src.app.traceback.adapters.opencti_adapter import OpenCTIAdapter
from src.app.traceback.adapters.registry import default_adapters
from src.app.traceback.schemas.attribution import ActorFeatures


# ---------- 三因子算法 ----------

def _actor(name, ttps, tools=(), iocs=()):
    return {"name": name, "ttps": list(ttps), "tools": list(tools), "iocs": list(iocs),
            "description": "", "sophistication": "", "primary_motivation": "",
            "goals": [], "campaigns": []}


def test_three_factor_scoring_ranks_correct_actor():
    """TTP(0.5)+IOC(0.3)+工具(0.2) 三因子应将最匹配的组织排第一"""
    from src.app.traceback.agents.attribution import AttributionAgent

    agent = AttributionAgent.__new__(AttributionAgent)
    features = ActorFeatures(
        ttps=["T1566.001", "T1059.001", "T1021.001", "T1003.001", "T1486"],
        tools=["powershell"],
        infrastructure=["203.0.113.55", "malicious-c2.example"],
    )
    actors = [
        _actor("RansomX", ["T1566.001", "T1059.001", "T1021.001", "T1003.001", "T1486", "T1071.001"],
               ["powershell"], ["203.0.113.55", "malicious-c2.example"]),
        _actor("APT-XX", ["T1566.002", "T1059.001", "T1547.001"], [], ["198.51.100.23"]),
        _actor("Miner", ["T1190", "T1496"], [], []),
    ]
    candidates = agent._score_candidates(features, actors)
    assert candidates[0].threat_actor == "RansomX"
    assert candidates[0].confidence > candidates[1].confidence
    # RansomX TTP 全命中 → score_ttp=1.0, IOC 全命中 → score_ioc=1.0
    assert candidates[0].score_ttp == 1.0
    assert candidates[0].score_ioc == 1.0
    # 置信度 = 100*(0.5*1 + 0.3*1 + 0.2*1) = 100
    assert candidates[0].confidence == 100.0


def test_attribution_threshold_no_weak_match():
    """低置信度(无 TTP/IOC 重叠)不应归因"""
    from src.app.traceback.agents.attribution import ATTRIBUTION_THRESHOLD, AttributionAgent

    agent = AttributionAgent.__new__(AttributionAgent)
    features = ActorFeatures(ttps=["T1486"], tools=[], infrastructure=[])
    actors = [_actor("Miner", ["T1190", "T1496"], [], [])]  # 无重叠
    candidates = agent._score_candidates(features, actors)
    assert candidates[0].confidence < ATTRIBUTION_THRESHOLD


# ---------- 特征提取 ----------

def test_feature_extraction_from_attack_chain():
    from src.app.traceback.agents.attribution import AttributionAgent

    agent = AttributionAgent.__new__(AttributionAgent)
    chain = {
        "ttp_summary": [{"technique_id": "T1566.001"}, {"technique_id": "T1486"},
                        {"raw_id": "T9999", "technique_id": None}],
        "attacker_ips": ["203.0.113.55"], "attacker_domains": ["malicious-c2.example"],
        "attacker_tools": ["PowerShell"], "data_exfiltration": False,
    }
    features = agent._extract_features(chain, [])
    assert "T1566.001" in features.ttps and "T1486" in features.ttps
    assert "T9999" not in features.ttps  # 未命中白名单的已 None 化, 不纳入
    assert "203.0.113.55" in features.infrastructure
    assert "powershell" in features.tools
    assert features.motive == "financial"  # T1486 → financial


def test_infer_attack_type():
    from src.app.traceback.agents.attribution import AttributionAgent

    agent = AttributionAgent.__new__(AttributionAgent)
    assert agent._infer_attack_type(ActorFeatures(ttps=["T1486"]), None) == "ransomware"
    assert agent._infer_attack_type(ActorFeatures(ttps=["T1041"]), None) == "apt"
    assert agent._infer_attack_type(ActorFeatures(ttps=["T1496"]), None) == "cryptomining"


# ---------- 相似案例 ----------

@pytest.mark.asyncio
async def test_similar_case_matches_ransomware():
    from src.app.traceback.services.similar_cases import SimilarCaseMatcher

    matcher = SimilarCaseMatcher()
    features = ActorFeatures(ttps=["T1566.001", "T1059.001", "T1486"])
    results = await matcher.search(features, attack_type="ransomware", top_k=3)
    assert len(results) > 0
    assert results[0]["attack_type"] == "ransomware"
    assert results[0]["similarity"] > 0.5
    assert "T1486" in results[0]["matched_ttps"]


# ---------- OpenCTI 种子降级 ----------

@pytest.mark.asyncio
async def test_opencti_seed_fallback():
    """无 OPENCTI_URL 时用种子威胁组织数据"""
    adapter = OpenCTIAdapter()
    await adapter.connect()
    actors = await adapter.get_actors()
    names = [a["name"] for a in actors]
    assert "RansomX Operator" in names
    # IOC 查询
    matched = await adapter.search_by_ioc("203.0.113.55")
    assert any("RansomX" in a["name"] for a in matched)
    await adapter.close()


# ---------- 归因黄金样本评测 (Top1 > 70%, §9.2) ----------

_GOLDEN = [
    {  # 勒索
        "ttps": ["T1566.001", "T1059.001", "T1021.001", "T1003.001", "T1486"],
        "tools": ["powershell"], "iocs": ["203.0.113.55", "malicious-c2.example"],
        "expected": "RansomX Operator",
    },
    {  # APT 窃密
        "ttps": ["T1566.002", "T1059.001", "T1547.001", "T1003.001", "T1041"],
        "tools": [], "iocs": ["198.51.100.23", "secure-update.example"],
        "expected": "APT-XX (Espionage)",
    },
    {  # 挖矿
        "ttps": ["T1190", "T1059.003", "T1496"],
        "tools": [], "iocs": ["203.0.113.99"],
        "expected": "CryptoMiner Gang",
    },
    {  # 勒索变体(部分 TTP)
        "ttps": ["T1566.001", "T1486", "T1003.001"],
        "tools": [], "iocs": [],
        "expected": "RansomX Operator",
    },
]


@pytest.mark.asyncio
async def test_attribution_top1_accuracy():
    """归因黄金样本 Top1 准确率 > 70% (§9.2 质量门)"""
    from src.app.traceback.agents.attribution import AttributionAgent

    agent = AttributionAgent.__new__(AttributionAgent)
    adapter = OpenCTIAdapter()
    await adapter.connect()
    actors = await adapter.get_actors()

    correct = 0
    for g in _GOLDEN:
        features = ActorFeatures(ttps=g["ttps"], tools=g["tools"], infrastructure=g["iocs"])
        candidates = agent._score_candidates(features, actors)
        top1 = candidates[0].threat_actor if candidates else None
        if top1 == g["expected"]:
            correct += 1
    accuracy = correct / len(_GOLDEN)
    assert accuracy > 0.7, f"归因 Top1 准确率 {accuracy:.0%} < 70%"
    await adapter.close()


# ---------- 深度取证适配器 ----------

@pytest.mark.asyncio
async def test_deep_adapters_mock_collect():
    """5 个深度适配器 mock 模式均返回证据"""
    from src.app.traceback.adapters.deep_adapters import deep_adapters

    from src.app.traceback.schemas.evidence import CollectContext

    ctx = CollectContext(task_id="t1", asset_ips=["10.0.0.5"])
    for name, adapter in deep_adapters().items():
        ok = await adapter.connect()
        assert ok, f"{name} 应可用"
        items = await adapter.collect(ctx)
        assert len(items) > 0, f"{name} mock 应返回证据"
        await adapter.close()


@pytest.mark.asyncio
async def test_deep_forensics_collector(monkeypatch):
    """DeepForensicsCollector 并行收集 5 源 → 存证 → deep_evidence_ids"""
    from src.app.traceback.agents import evidence_collector as ec_mod  # noqa: F401
    from src.app.traceback.services import deep_forensics as df_mod
    from src.app.traceback.services.deep_forensics import DeepForensicsCollector

    async def fake_get_task(session, task_id):
        return {"task_id": task_id, "asset_ips": ["10.0.0.5"], "asset_hosts": []}

    @asynccontextmanager
    async def fake_session():
        yield None

    saved: list[dict] = []

    async def fake_save(session, **kw):
        eid = f"deep-{len(saved)+1:02d}"
        saved.append(kw)
        return eid

    monkeypatch.setattr(df_mod.task_repo, "get_task", fake_get_task)
    monkeypatch.setattr(df_mod, "session_scope", fake_session)
    monkeypatch.setattr(df_mod.evidence_repo, "save_evidence", fake_save)

    manager = ComponentManager()
    for n, a in default_adapters().items():
        manager.register(n, a)
    await manager.initialize()

    collector = DeepForensicsCollector(manager=manager)
    result = await collector.run({"task_id": "t-deep", "stage": "collecting"})
    assert len(result["deep_evidence_ids"]) >= 5  # 5 源各至少 1 条
    sources = {s["source"] for s in saved}
    assert {"zeek", "velociraptor", "yara", "cape", "volatility"} <= sources
    await manager.close_all()


# ---------- UAT-002 mini: 攻击链 → 归因(APT 识别) ----------

@pytest.mark.asyncio
async def test_uat002_attribution_identifies_actor(monkeypatch):
    """UAT-002: APT 攻击链 → 归因正确识别威胁组织"""
    from src.app.traceback.agents.attribution import AttributionAgent

    # 注入 OpenCTI 种子(无需 DB); 跳过持久化
    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_save_chain(session, task_id, payload):
        return "chain-id"

    async def fake_save_attr(session, task_id, payload):
        return "attr-id"

    monkeypatch.setattr(
        "src.app.traceback.agents.attribution.session_scope", fake_session
    )
    monkeypatch.setattr(
        "src.app.traceback.agents.attribution.attack_chain_repo.get_chain",
        lambda session, tid: None,  # 强制用 state 里的 attack_chain
    )
    monkeypatch.setattr(
        "src.app.traceback.agents.attribution.attack_chain_repo.save_chain", fake_save_chain
    )
    monkeypatch.setattr(
        "src.app.traceback.agents.attribution.attribution_repo.save_attribution", fake_save_attr
    )

    manager = ComponentManager()
    manager.register("opencti", OpenCTIAdapter())
    await manager.initialize()

    agent = AttributionAgent(manager=manager)
    # APT 攻击链
    state = {
        "task_id": "t-uat2", "stage": "chain_analyzed",
        "attack_chain": {
            "ttp_summary": [{"technique_id": "T1566.002"}, {"technique_id": "T1059.001"},
                            {"technique_id": "T1547.001"}, {"technique_id": "T1003.001"},
                            {"technique_id": "T1041"}],
            "attacker_ips": ["198.51.100.23"], "attacker_domains": ["secure-update.example"],
            "attacker_tools": [], "data_exfiltration": True,
        },
        "timeline_events": [],
    }
    result = await agent.run(state)
    attr = result["attribution"]
    assert attr["attributed"] is True
    assert attr["threat_actor"] == "APT-XX (Espionage)"
    assert attr["confidence_score"] > 0
    assert attr["actor_profile"] is not None
    assert len(result["similar_cases"]) > 0
    await manager.close_all()
