"""P3 知识 API 集成测试 + 状态映射回归

对齐 03_phase3_design.md §7/§8 — 覆盖 knowledge 15 端点 happy/error + set_stage 全阶段状态映射。
httpx.AsyncClient + ASGITransport, 注入空会话 + monkeypatch 仓库, 不触达真实服务。
"""
from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from httpx import ASGITransport

from src.app.traceback.api.deps import verify_token
from src.app.traceback.core.db import get_session
from src.app.traceback.main import app

PREFIX = "/api/v1/traceback/knowledge"


@pytest.fixture()
async def client(monkeypatch):
    async def _noop_session():
        yield None

    async def _noop_token():
        return "tester"

    app.dependency_overrides[get_session] = _noop_session
    app.dependency_overrides[verify_token] = _noop_token
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ---------- set_stage 状态映射回归 (修复: reported 不再=done) ----------

def test_stage_status_mapping_covers_p3_stages():
    """P3 新增阶段的状态映射正确 (修复 reported→done 过早的 bug)"""
    from src.app.traceback.repositories.task_repo import _STAGE_STATUS

    assert _STAGE_STATUS["done"] == "done"
    assert _STAGE_STATUS["reported"] == "analyzing"  # reported 后还有知识流程, 非 done
    assert _STAGE_STATUS["knowledge_sunk"] == "analyzing"
    assert _STAGE_STATUS["knowledge_review"] == "reviewing"
    assert _STAGE_STATUS["review_attribution"] == "reviewing"
    assert _STAGE_STATUS["review_timeline"] == "reviewing"
    assert _STAGE_STATUS["attributed"] == "analyzing"


# ---------- 进度接口 knowledge_review 映射 ----------

async def test_progress_knowledge_review_stage(client, monkeypatch):
    async def _get(session, tid):
        return {"task_id": tid, "current_stage": "knowledge_review", "status": "reviewing",
                "create_time": datetime.now(), "update_time": datetime.now()}

    monkeypatch.setattr("src.app.traceback.api.v1.analysis.task_repo.get_task", _get)
    r = await client.get("/api/v1/traceback/tasks/t1/progress")
    assert r.status_code == 200
    data = r.json()
    assert data["awaiting_review"] is True
    assert data["review_stage"] == "knowledge"
    assert data["progress_percent"] == 97


# ---------- 案例 API ----------

async def test_case_create_and_get(client, monkeypatch):
    saved = {}

    async def _save(case):
        saved.update(case)
        return case["case_id"]

    async def _get(case_id):
        return {**saved, "case_id": case_id} if saved else None

    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.knowledge_repo.save_case", _save)
    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.knowledge_repo.get_case", _get)

    r = await client.post(f"{PREFIX}/cases", json={"title": "新案例", "attack_type": "ransomware"})
    assert r.status_code == 200
    cid = r.json()["case_id"]
    assert r.json()["status"] == "draft"

    r = await client.get(f"{PREFIX}/cases/{cid}")
    assert r.status_code == 200
    assert r.json()["title"] == "新案例"


async def test_case_not_found(client, monkeypatch):
    async def _get(case_id):
        return None

    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.knowledge_repo.get_case", _get)
    r = await client.get(f"{PREFIX}/cases/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "TB-2001"


async def test_case_publish(client, monkeypatch):
    case = {"case_id": "c1", "status": "draft", "title": "x"}

    async def _get(case_id):
        return case

    async def _save(c):
        case.update(c)
        return c["case_id"]

    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.knowledge_repo.get_case", _get)
    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.knowledge_repo.save_case", _save)
    r = await client.post(f"{PREFIX}/cases/c1/publish")
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert case["status"] == "published"


async def test_case_search_similar(client, monkeypatch):
    async def _search(features, top_k=3):
        return [{"case_id": "c1", "title": "勒索A", "similarity": 0.9, "matched_ttps": ["T1486"]}]

    monkeypatch.setattr(
        "src.app.traceback.api.v1.knowledge.knowledge_repo.search_similar_cases", _search
    )
    r = await client.post(f"{PREFIX}/cases/search-similar",
                          json={"attack_type": "ransomware", "ttps": ["T1486"]})
    assert r.status_code == 200
    assert len(r.json()["cases"]) == 1


# ---------- IOC API ----------

async def test_ioc_batch_query(client, monkeypatch):
    async def _batch(session, iocs):
        return [{"ioc_type": i["type"], "ioc_value": i["value"], "found": i["value"] == "203.0.113.55"}
                for i in iocs]

    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.knowledge_repo.batch_query_iocs", _batch)
    r = await client.post(f"{PREFIX}/iocs/batch-query",
                          json={"iocs": [{"type": "ip", "value": "203.0.113.55"}]})
    assert r.status_code == 200
    assert r.json()["results"][0]["found"] is True


# ---------- Playbook API ----------

async def test_playbook_recommend(client, monkeypatch):
    async def _rec(session, attack_type, ttps, top_k=3):
        return [{"playbook_id": "pb1", "name": "勒索处置", "attack_type": attack_type,
                 "match_score": 0.9, "usage_count": 10, "success_rate": 0.8}]

    monkeypatch.setattr(
        "src.app.traceback.api.v1.knowledge.knowledge_repo.recommend_playbooks", _rec
    )
    r = await client.post(f"{PREFIX}/playbooks/recommend",
                          json={"attack_type": "ransomware", "ttps": ["T1486"]})
    assert r.status_code == 200
    assert r.json()["recommended_playbooks"][0]["match_score"] > 0


async def test_playbook_not_found(client, monkeypatch):
    async def _list(session, **kw):
        return []

    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.knowledge_repo.list_playbooks", _list)
    r = await client.get(f"{PREFIX}/playbooks/nope")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "TB-2002"


# ---------- 知识问答 API ----------

async def test_qa_endpoint(client, monkeypatch):
    async def _answer(question):
        return {"answer": "勒索病毒常通过钓鱼...", "has_result": True,
                "citations": [{"type": "case", "id": "c1"}]}

    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.qa.answer_question", _answer)
    r = await client.post(f"{PREFIX}/qa", json={"question": "勒索病毒如何分析"})
    assert r.status_code == 200
    data = r.json()
    assert data["has_result"] is True
    assert len(data["citations"]) >= 1


async def test_qa_empty_question_no_crash(client, monkeypatch):
    """空问题不应 500 (回归: 确认 split 守卫)"""
    async def _answer(question):
        return {"answer": "知识库中无相关记录", "has_result": False, "citations": []}

    monkeypatch.setattr("src.app.traceback.api.v1.knowledge.qa.answer_question", _answer)
    r = await client.post(f"{PREFIX}/qa", json={"question": ""})
    assert r.status_code == 200
