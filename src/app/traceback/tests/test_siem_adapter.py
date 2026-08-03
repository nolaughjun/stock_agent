"""SIEM 适配器真实路径单测 (mock httpx)

对齐 01_phase1_design.md §7.2 — 验证 OpenSearch DSL 查询路径(非 mock 模式)。
通过注入 mock httpx.AsyncClient, 验证: DSL 构造、查询执行、结果解析、错误降级。
"""
from __future__ import annotations

from typing import Any

import pytest

from src.app.traceback.adapters.siem_adapter import SIEMAdapter
from src.app.traceback.schemas.evidence import CollectContext


class _MockResponse:
    def __init__(self, status_code: int, json_data: Any = None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _MockAsyncClient:
    """最小 httpx.AsyncClient mock, 记录请求并返回预设响应"""

    def __init__(self, responses: dict[str, _MockResponse]):
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def get(self, path, **kw):
        self.calls.append(("GET", path, None))
        return self._responses.get(path, _MockResponse(404))

    async def post(self, path, json=None, **kw):
        self.calls.append(("POST", path, json))
        return self._responses.get(path, _MockResponse(404))

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_siem_real_path_query_and_parse(monkeypatch):
    """真实模式: 配置 base_url → DSL 查询 → 解析 hits 为证据"""
    monkeypatch.setattr("src.app.traceback.adapters.siem_adapter.settings.siem_base_url", "http://opensearch:9200")
    monkeypatch.setattr("src.app.traceback.adapters.siem_adapter.settings.siem_api_key", "key")

    adapter = SIEMAdapter()
    # 注入 mock client (跳过真实 connect)
    mock = _MockAsyncClient({
        "/_cluster/health": _MockResponse(200),
        "/logs-*/_search": _MockResponse(200, {
            "hits": {"hits": [
                {"_source": {"source.ip": "10.0.0.5", "destination.ip": "203.0.113.55",
                             "@timestamp": "2026-07-01T08:00:00Z", "event.action": "connection"}},
                {"_source": {"source.ip": "10.0.0.5", "event.action": "powershell"}},
            ]}
        }),
    })
    adapter._client = mock
    adapter._mock = False

    ctx = CollectContext(task_id="t1", asset_ips=["10.0.0.5"])
    items = await adapter.collect(ctx)

    # 发出了 _search 请求
    assert any(c[1] == "/logs-*/_search" for c in mock.calls)
    # DSL 含 source.ip 过滤
    dsl = next(c[2] for c in mock.calls if c[1] == "/logs-*/_search")
    assert "source.ip" in str(dsl)
    # 解析为 2 条证据
    assert len(items) == 2
    assert all(it.evidence_type == "log" for it in items)
    # related_assets 从 source/destination 提取
    assets0 = items[0].related_assets
    assert "10.0.0.5" in assets0 and "203.0.113.55" in assets0


@pytest.mark.asyncio
async def test_siem_real_path_degrades_on_error(monkeypatch):
    """真实路径查询异常 → 降级 mock (TR-002 对策)"""
    monkeypatch.setattr("src.app.traceback.adapters.siem_adapter.settings.siem_base_url", "http://opensearch:9200")
    monkeypatch.setattr("src.app.traceback.adapters.siem_adapter.settings.siem_api_key", None)
    # mock_data 默认返回 canned 勒索日志
    monkeypatch.setattr("src.app.traceback.adapters.siem_adapter.settings.evidence_mock_mode", True)

    adapter = SIEMAdapter()
    adapter._mock = False
    adapter._client = None  # 触发 _client is None 分支 → 走 mock
    ctx = CollectContext(task_id="t1", asset_ips=["10.0.0.5"])
    items = await adapter.collect(ctx)
    assert len(items) > 0  # 降级 mock 返回数据


@pytest.mark.asyncio
async def test_siem_health_check_real(monkeypatch):
    monkeypatch.setattr("src.app.traceback.adapters.siem_adapter.settings.siem_base_url", "http://opensearch:9200")
    adapter = SIEMAdapter()
    adapter._mock = False
    adapter._client = _MockAsyncClient({"/_cluster/health": _MockResponse(200)})
    assert await adapter.health_check() is True

    adapter._client = _MockAsyncClient({"/_cluster/health": _MockResponse(500)})
    assert await adapter.health_check() is False
