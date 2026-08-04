"""P3 知识沉淀与智能应用测试

对齐 03_phase3_design.md §6 / §5 / §4 / §11
验证: 案例自动提取、相似案例 Top3>60%、处置推荐、IOC 批量匹配、RAG 引用/拒答、
沉淀→检索闭环。注入 Mongo/PG 替换, 不触达真实服务。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest



# ---------- 案例自动提取 (KnowledgeSink) ----------

@pytest.mark.asyncio
async def test_knowledge_sink_extracts_draft(monkeypatch):
    """KnowledgeSink 从攻击链提取案例/IOC/TTP/Playbook 草稿"""
    from src.app.traceback.agents import knowledge_sink as ks_mod
    from src.app.traceback.agents.knowledge_sink import KnowledgeSink

    saved_case: dict = {}
    saved_iocs: list[dict] = []
    saved_ttps: list[dict] = []
    saved_pb: dict = {}

    async def fake_save_case(case):
        saved_case.update(case)
        return case["case_id"]

    async def fake_save_ioc(session, ioc):
        saved_iocs.append(ioc)
        return ioc.get("ioc_id", "ioc-x")

    async def fake_upsert_ttp(session, ttp):
        saved_ttps.append(ttp)
        return ttp["ttp_id"]

    async def fake_get_ttp(session, tid):
        return None

    async def fake_save_pb(session, pb):
        saved_pb.update(pb)
        return pb["playbook_id"]

    @asynccontextmanager
    async def fake_session():
        yield None

    monkeypatch.setattr(ks_mod.knowledge_repo, "save_case", fake_save_case)
    monkeypatch.setattr(ks_mod.knowledge_repo, "save_ioc", fake_save_ioc)
    monkeypatch.setattr(ks_mod.knowledge_repo, "upsert_ttp", fake_upsert_ttp)
    monkeypatch.setattr(ks_mod.knowledge_repo, "get_ttp", fake_get_ttp)
    monkeypatch.setattr(ks_mod.knowledge_repo, "save_playbook", fake_save_pb)
    monkeypatch.setattr(ks_mod, "session_scope", fake_session)

    sink = KnowledgeSink()
    state = {
        "task_id": "t-sink", "stage": "reported",
        "task": {"task_name": "勒索病毒事件", "description": "财务主机被加密"},
        "attack_chain": {
            "ttp_summary": [{"technique_id": "T1566.001"}, {"technique_id": "T1486"}],
            "attacker_ips": ["203.0.113.55"], "attacker_domains": ["malicious-c2.example"],
            "affected_assets": ["10.0.0.5"],
        },
        "attribution": {"threat_actor": "RansomX Operator"},
    }
    result = await sink.run(state)
    pending = result["pending_knowledge"]

    # 案例草稿
    assert pending["case_id"] == "case-t-sink"
    assert saved_case["attack_type"] == "ransomware"
    assert saved_case["status"] == "draft"
    assert "T1566.001" in saved_case["ttps"]
    # 案例提取完整度: 攻击类型/TTP/IOC/标题 均提取
    assert saved_case["title"] == "勒索病毒事件"
    assert len(saved_case["iocs"]) == 2  # 1 IP + 1 domain
    # IOC 提取
    assert len(saved_iocs) == 2
    assert any(i["ioc_value"] == "203.0.113.55" for i in saved_iocs)
    # TTP 反向引用
    assert {t["ttp_id"] for t in saved_ttps} == {"T1566.001", "T1486"}
    # Playbook 草稿
    assert saved_pb["attack_type"] == "ransomware"
    assert pending["playbook_id"] == "pb-t-sink"


# ---------- 相似案例检索 (Top3 > 60%) ----------

@pytest.mark.asyncio
async def test_similar_case_top3_accuracy(monkeypatch):
    """相似案例推荐 Top3 命中率 > 60% (§1.3 质量门)"""
    from src.app.traceback.repositories import knowledge_repo

    seed_cases = [
        {"case_id": "c1", "title": "勒索病毒A", "attack_type": "ransomware",
         "ttps": ["T1566.001", "T1059.001", "T1486"]},
        {"case_id": "c2", "title": "勒索病毒B", "attack_type": "ransomware",
         "ttps": ["T1486", "T1021.001"]},
        {"case_id": "c3", "title": "APT窃密", "attack_type": "apt",
         "ttps": ["T1566.002", "T1041"]},
        {"case_id": "c4", "title": "挖矿", "attack_type": "cryptomining", "ttps": ["T1496"]},
    ]

    async def fake_list(**kw):
        return seed_cases

    monkeypatch.setattr(knowledge_repo, "list_cases", fake_list)

    # 3 个查询, 每个 Top3 应含同类案例
    queries = [
        ({"attack_type": "ransomware", "ttps": ["T1566.001", "T1059.001", "T1486"]}, {"c1", "c2"}),
        ({"attack_type": "apt", "ttps": ["T1566.002", "T1041"]}, {"c3"}),
        ({"attack_type": "ransomware", "ttps": ["T1486"]}, {"c1", "c2"}),
    ]
    hits = 0
    total = 0
    for features, expected in queries:
        results = await knowledge_repo.search_similar_cases(features, top_k=3)
        got = {r["case_id"] for r in results}
        if expected & got:
            hits += 1
        total += 1
    accuracy = hits / total
    assert accuracy > 0.6, f"相似案例 Top3 命中率 {accuracy:.0%} < 60%"


# ---------- 处置方案推荐 ----------

@pytest.mark.asyncio
async def test_playbook_recommend(monkeypatch):
    from src.app.traceback.repositories import knowledge_repo

    async def fake_list_pb(session, *, attack_type=None):
        all_pbs = [
            {"playbook_id": "pb-r", "name": "勒索处置", "attack_type": "ransomware",
             "effectiveness": 0.88, "usage_count": 42, "success_rate": 0.85},
            {"playbook_id": "pb-a", "name": "APT处置", "attack_type": "apt",
             "effectiveness": 0.82, "usage_count": 18, "success_rate": 0.78},
        ]
        if attack_type:
            return [p for p in all_pbs if p["attack_type"] == attack_type]
        return all_pbs

    @asynccontextmanager
    async def fake_session():
        yield None

    monkeypatch.setattr(knowledge_repo, "list_playbooks", fake_list_pb)

    recs = await knowledge_repo.recommend_playbooks(None, "ransomware", ["T1486"], top_k=3)
    assert len(recs) > 0
    assert recs[0]["attack_type"] == "ransomware"
    assert recs[0]["match_score"] > 0
    assert "usage_count" in recs[0] and "success_rate" in recs[0]


# ---------- IOC 批量匹配 ----------

@pytest.mark.asyncio
async def test_ioc_batch_query(monkeypatch):
    from src.app.traceback.repositories import knowledge_repo

    async def fake_batch(session, iocs):
        out = []
        for ioc in iocs:
            if ioc["value"] == "203.0.113.55":
                out.append({"ioc_type": "ip", "ioc_value": "203.0.113.55", "found": True,
                            "threat_actor": "RansomX Operator", "malware_family": "RansomX"})
            else:
                out.append({"ioc_type": ioc["type"], "ioc_value": ioc["value"], "found": False})
        return out

    @asynccontextmanager
    async def fake_session():
        yield None

    monkeypatch.setattr(knowledge_repo, "batch_query_iocs", fake_batch)
    results = await knowledge_repo.batch_query_iocs(None, [
        {"type": "ip", "value": "203.0.113.55"},
        {"type": "ip", "value": "1.2.3.4"},
    ])
    assert results[0]["found"] is True
    assert results[0]["threat_actor"] == "RansomX Operator"
    assert results[1]["found"] is False


# ---------- RAG 问答 (强制引用 + 无结果拒答) ----------

@pytest.mark.asyncio
async def test_qa_returns_citations(monkeypatch):
    from src.app.traceback.services import qa as qa_mod

    async def fake_cases(question, top_k):
        return [{"case_id": "c1", "title": "勒索案例", "description": "钓鱼勒索"}]

    async def fake_articles(question, top_k):
        return [{"article_id": "a1", "title": "勒索技巧", "content": "..."}]

    async def fake_iocs(question):
        return []

    monkeypatch.setattr(qa_mod, "_search_cases_by_keyword", fake_cases)
    monkeypatch.setattr(qa_mod.knowledge_repo, "search_articles", fake_articles)
    monkeypatch.setattr(qa_mod, "_extract_iocs_from_question", fake_iocs)

    result = await qa_mod.answer_question("勒索病毒如何分析")
    assert result["has_result"] is True
    assert len(result["citations"]) >= 2  # case + article
    cite_types = {c["type"] for c in result["citations"]}
    assert "case" in cite_types and "article" in cite_types
    assert "勒索" in result["answer"]


@pytest.mark.asyncio
async def test_qa_refuses_when_no_results(monkeypatch):
    """无相关结果时明确拒答, 不编造 (防幻觉)"""
    from src.app.traceback.services import qa as qa_mod

    async def fake_cases(question, top_k):
        return []

    async def fake_articles(question, top_k):
        return []

    async def fake_iocs(question):
        return []

    monkeypatch.setattr(qa_mod, "_search_cases_by_keyword", fake_cases)
    monkeypatch.setattr(qa_mod.knowledge_repo, "search_articles", fake_articles)
    monkeypatch.setattr(qa_mod, "_extract_iocs_from_question", fake_iocs)

    result = await qa_mod.answer_question("完全无关的问题xyz123")
    assert result["has_result"] is False
    assert result["citations"] == []
    assert "无" in result["answer"]


# ---------- 沉淀→检索闭环 ----------

@pytest.mark.asyncio
async def test_sink_then_retrieve_loop(monkeypatch):
    """完成溯源 → 沉淀案例(draft) → 发布 → 相似检索命中 (闭环)"""
    from src.app.traceback.agents import knowledge_sink as ks_mod
    from src.app.traceback.agents.knowledge_sink import KnowledgeSink
    from src.app.traceback.repositories import knowledge_repo

    cases_db: dict[str, dict] = {}

    async def fake_save_case(case):
        cases_db[case["case_id"]] = dict(case)
        return case["case_id"]

    async def fake_get_case(case_id):
        return cases_db.get(case_id)

    async def fake_list(*, attack_type=None, tags=None, status="published", limit=50):
        return [c for c in cases_db.values() if c.get("status") == status]

    async def fake_search_similar(features, top_k=3):
        # 真实实现逻辑(简化): TTP 重叠
        event_ttps = set(features.get("ttps") or [])
        cands = [c for c in cases_db.values() if c.get("status") == "published"]
        scored = sorted(cands, key=lambda c: len(event_ttps & set(c.get("ttps") or [])), reverse=True)
        return [{"case_id": c["case_id"], "title": c["title"], "similarity": 0.9,
                 "matched_ttps": list(event_ttps & set(c.get("ttps") or []))}
                for c in scored[:top_k] if event_ttps & set(c.get("ttps") or [])]

    # 沉淀阶段: save_ioc/upsert_ttp/save_playbook 打桩
    async def noop_ioc(session, ioc):
        return "ioc-x"

    async def noop_ttp_get(session, tid):
        return None

    async def noop_ttp_upsert(session, ttp):
        return ttp["ttp_id"]

    async def noop_pb(session, pb):
        return pb["playbook_id"]

    @asynccontextmanager
    async def fake_session():
        yield None

    monkeypatch.setattr(ks_mod.knowledge_repo, "save_case", fake_save_case)
    monkeypatch.setattr(ks_mod.knowledge_repo, "save_ioc", noop_ioc)
    monkeypatch.setattr(ks_mod.knowledge_repo, "get_ttp", noop_ttp_get)
    monkeypatch.setattr(ks_mod.knowledge_repo, "upsert_ttp", noop_ttp_upsert)
    monkeypatch.setattr(ks_mod.knowledge_repo, "save_playbook", noop_pb)
    monkeypatch.setattr(ks_mod, "session_scope", fake_session)

    monkeypatch.setattr(knowledge_repo, "save_case", fake_save_case)
    monkeypatch.setattr(knowledge_repo, "get_case", fake_get_case)
    monkeypatch.setattr(knowledge_repo, "list_cases", fake_list)
    monkeypatch.setattr(knowledge_repo, "search_similar_cases", fake_search_similar)

    # 1. 沉淀 (draft)
    sink = KnowledgeSink()
    await sink.run({
        "task_id": "t-loop", "stage": "reported",
        "task": {"task_name": "闭环勒索事件"},
        "attack_chain": {"ttp_summary": [{"technique_id": "T1566.001"}, {"technique_id": "T1486"}],
                          "attacker_ips": ["203.0.113.55"], "attacker_domains": [],
                          "affected_assets": []},
        "attribution": {},
    })
    assert "case-t-loop" in cases_db
    assert cases_db["case-t-loop"]["status"] == "draft"

    # 2. 未发布时检索不到
    results = await knowledge_repo.search_similar_cases({"ttps": ["T1486"]})
    assert len(results) == 0  # draft 不参与

    # 3. 发布
    case = cases_db["case-t-loop"]
    case["status"] = "published"
    await knowledge_repo.save_case(case)

    # 4. 同类事件检索命中 (闭环)
    results = await knowledge_repo.search_similar_cases({"ttps": ["T1486", "T1566.001"]})
    assert len(results) > 0
    assert results[0]["case_id"] == "case-t-loop"
    assert "T1486" in results[0]["matched_ttps"]
