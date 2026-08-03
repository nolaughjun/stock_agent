"""API 集成测试 (httpx.AsyncClient + ASGITransport)

对齐 01_phase1_design.md §6 — 覆盖任务 CRUD / 进度 / 审核 / 报告下载。
ASGITransport 不触发 lifespan(避免适配器/checkpointer 初始化), 通过 dependency_overrides
注入空会话 + monkeypatch 仓库, 不触达真实 DB。
"""
from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from httpx import ASGITransport

from src.app.traceback.api.deps import verify_token
from src.app.traceback.core.db import get_session
from src.app.traceback.main import app

PREFIX = "/api/v1/traceback"


def _fake_task(task_id: str = "task-1", stage: str = "created", status: str = "created") -> dict:
    return {
        "task_id": task_id, "task_name": "测试事件", "description": "d",
        "status": status, "current_stage": stage,
        "alert_ids": [], "asset_ips": ["10.0.0.1"], "asset_hosts": [], "asset_users": [],
        "event_start_time": None, "event_end_time": None, "assignee": None,
        "retry_count": 0, "error_message": None,
        "create_time": datetime.now(), "update_time": datetime.now(),
    }


@pytest.fixture()
async def client(monkeypatch):
    # 跳过鉴权 + 注入空会话 (仓库函数被 monkeypatch, 不真正使用 session)
    async def _noop_session():
        yield None

    async def _noop_token():
        return "tester"

    app.dependency_overrides[get_session] = _noop_session
    app.dependency_overrides[verify_token] = _noop_token

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ---------- 任务 CRUD ----------

async def test_create_task(client, monkeypatch):
    async def _create(session, payload):
        return "task-new"

    async def _get(session, tid):
        return _fake_task(tid, "created")

    monkeypatch.setattr("src.app.traceback.api.v1.tasks.task_repo.create_task", _create)
    monkeypatch.setattr("src.app.traceback.api.v1.tasks.task_repo.get_task", _get)
    r = await client.post(f"{PREFIX}/tasks", json={"task_name": "勒索事件", "asset_ips": ["10.0.0.1"]})
    assert r.status_code == 201
    assert r.json()["task_id"] == "task-new"


async def test_create_task_validation_error(client):
    r = await client.post(f"{PREFIX}/tasks", json={"task_name": ""})
    assert r.status_code == 422


async def test_get_task_not_found(client, monkeypatch):
    async def _get(session, tid):
        return None

    monkeypatch.setattr("src.app.traceback.api.v1.tasks.task_repo.get_task", _get)
    r = await client.get(f"{PREFIX}/tasks/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "TB-1001"


async def test_list_tasks(client, monkeypatch):
    async def _list(session, **kw):
        return [_fake_task("a"), _fake_task("b")], 2

    monkeypatch.setattr("src.app.traceback.api.v1.tasks.task_repo.list_tasks", _list)
    r = await client.get(f"{PREFIX}/tasks")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2 and len(data["items"]) == 2


async def test_cancel_task(client, monkeypatch):
    async def _get(session, tid):
        return _fake_task(tid)

    async def _cancel(session, tid):
        return True

    monkeypatch.setattr("src.app.traceback.api.v1.tasks.task_repo.get_task", _get)
    monkeypatch.setattr("src.app.traceback.api.v1.tasks.task_repo.cancel_task", _cancel)
    r = await client.delete(f"{PREFIX}/tasks/task-1")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


# ---------- 进度 ----------

async def test_progress(client, monkeypatch):
    async def _get(session, tid):
        return _fake_task(tid, stage="review_timeline", status="reviewing")

    monkeypatch.setattr("src.app.traceback.api.v1.analysis.task_repo.get_task", _get)
    r = await client.get(f"{PREFIX}/tasks/task-1/progress")
    assert r.status_code == 200
    data = r.json()
    assert data["awaiting_review"] is True
    assert data["review_stage"] == "timeline"
    assert data["progress_percent"] == 45


# ---------- 审核 ----------

async def test_review_wrong_stage(client, monkeypatch):
    async def _get(session, tid):
        return _fake_task(tid, stage="collecting")

    monkeypatch.setattr("src.app.traceback.api.v1.analysis.task_repo.get_task", _get)
    r = await client.post(
        f"{PREFIX}/tasks/task-1/review",
        json={"stage": "timeline", "action": "approve"},
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "TB-1002"


async def test_review_ok_triggers_resume(client, monkeypatch):
    async def _get(session, tid):
        return _fake_task(tid, stage="review_timeline", status="reviewing")

    async def _noop_add_review(session, *args, **kwargs):
        return "rev-1"

    async def _noop_dispatch(task_id, decision):
        return None

    monkeypatch.setattr("src.app.traceback.api.v1.analysis.task_repo.get_task", _get)
    monkeypatch.setattr("src.app.traceback.api.v1.analysis.task_repo.add_review", _noop_add_review)
    monkeypatch.setattr("src.app.traceback.api.v1.analysis.task_runner.dispatch_resume", _noop_dispatch)
    r = await client.post(
        f"{PREFIX}/tasks/task-1/review",
        json={"stage": "timeline", "action": "approve", "reviewer": "alice"},
    )
    assert r.status_code == 200 and r.json()["review_id"] == "rev-1"


# ---------- 报告下载 ----------

async def test_report_not_found(client, monkeypatch):
    async def _get(session, tid):
        return _fake_task(tid, stage="reported", status="done")

    async def _none(task_id, fmt):
        return None

    monkeypatch.setattr("src.app.traceback.api.v1.results.task_repo.get_task", _get)
    monkeypatch.setattr("src.app.traceback.services.report_service.get_report_from_minio", _none)
    r = await client.get(f"{PREFIX}/tasks/task-1/report?fmt=pdf")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "TB-1005"


async def test_report_download_ok(client, monkeypatch):
    async def _get(session, tid):
        return _fake_task(tid, stage="reported", status="done")

    async def _pdf(task_id, fmt):
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("src.app.traceback.api.v1.results.task_repo.get_task", _get)
    monkeypatch.setattr("src.app.traceback.services.report_service.get_report_from_minio", _pdf)
    r = await client.get(f"{PREFIX}/tasks/task-1/report?fmt=pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


async def test_report_bad_format(client, monkeypatch):
    async def _get(session, tid):
        return _fake_task(tid, stage="reported", status="done")

    monkeypatch.setattr("src.app.traceback.api.v1.results.task_repo.get_task", _get)
    r = await client.get(f"{PREFIX}/tasks/task-1/report?fmt=html")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "TB-1004"
