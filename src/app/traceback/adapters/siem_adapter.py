"""SIEM 适配器

对齐 01_phase1_design.md §7.2 — 按 asset+时间查日志 (OpenSearch DSL)。
- 真实模式: httpx 调 OpenSearch _search, DSL 经白名单字段校验防注入
- mock 模式: 无 siem_base_url 时返回 canned 勒索病毒日志 (TR-002 对策)
"""
from __future__ import annotations

from typing import Any

import httpx

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.adapters.mock_data import get_mock_evidence
from src.app.traceback.core.config import settings
from src.app.traceback.core.logging import logger
from src.app.traceback.schemas.evidence import CollectContext, RawEvidence

# OpenSearch 查询字段白名单 (防注入, §8.2)
_ALLOWED_FIELDS = {
    "source.ip", "destination.ip", "source.host", "destination.host",
    "user.name", "event.action", "@timestamp", "event.module",
}


class SIEMAdapter(BaseAdapter):
    name = "siem"

    def __init__(self) -> None:
        self.base_url = settings.siem_base_url
        self.api_key = settings.siem_api_key
        self._client: httpx.AsyncClient | None = None
        self._mock = not self.base_url  # 无真实配置 → mock 模式

    async def connect(self) -> bool:
        # mock 模式始终可用 (collect 内部返回 mock 数据); 真实模式建立 client
        if self._mock:
            return True
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            timeout=settings.evidence_collect_timeout,
        )
        return True

    async def health_check(self) -> bool:
        if self._mock:
            return True
        if not self._client:
            return False
        try:
            r = await self._client.get("/_cluster/health")
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        if self._mock or not self._client:
            return get_mock_evidence("siem", ctx)
        # 真实模式: 构造 DSL 查询
        dsl = self._build_dsl(ctx)
        try:
            r = await self._client.post("/logs-*/_search", json=dsl)
            r.raise_for_status()
            hits = r.json().get("hits", {}).get("hits", [])
            return [
                RawEvidence(
                    evidence_type="log", source="siem",
                    raw=h["_source"],
                    parsed=_extract_fields(h["_source"]),
                    related_assets=_extract_assets(h["_source"]),
                    importance="medium",
                )
                for h in hits[: settings.max_log_per_task]
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("SIEM 查询失败, 降级 mock: {}", str(e)[:120])
            self.available = False
            return get_mock_evidence("siem", ctx)

    def _build_dsl(self, ctx: CollectContext) -> dict[str, Any]:
        """构造 OpenSearch DSL (仅用白名单字段)"""
        must: list[dict[str, Any]] = []
        if ctx.asset_ips:
            must.append({"terms": {"source.ip": ctx.asset_ips}})
        if ctx.event_start_time or ctx.event_end_time:
            rng: dict[str, Any] = {}
            if ctx.event_start_time:
                rng["gte"] = ctx.event_start_time.isoformat()
            if ctx.event_end_time:
                rng["lte"] = ctx.event_end_time.isoformat()
            must.append({"range": {"@timestamp": rng}})
        return {"query": {"bool": {"must": must or [{"match_all": {}}]}},
                "size": settings.llm_batch_size * 4}


def _extract_fields(src: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in src.items() if k in _ALLOWED_FIELDS}


def _extract_assets(src: dict[str, Any]) -> list[str]:
    assets: list[str] = []
    for f in ("source.ip", "destination.ip", "source.host", "destination.host"):
        v = src.get(f)
        if v:
            assets.append(str(v))
    return assets
