"""MISP 威胁情报适配器

对齐 traceback_system_prd.md §9.16.2-C — IP/域名/Hash 信誉查询。
- 真实模式: pymisp 查询
- mock 模式: 无 misp_url 时返回 canned IOC 信誉 (TR-002 对策)
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.adapters.mock_data import get_mock_evidence
from src.app.traceback.core.config import settings
from src.app.traceback.core.logging import logger
from src.app.traceback.schemas.evidence import CollectContext, RawEvidence

IOCType = str  # "ip" | "domain" | "hash"


class MISPAdapter(BaseAdapter):
    name = "misp"

    def __init__(self) -> None:
        self.url = settings.misp_url
        self.api_key = settings.misp_api_key
        self._misp: Any = None
        self._mock = not self.url  # 无真实配置 → mock 模式

    async def connect(self) -> bool:
        if self._mock:
            return True
        try:
            from pymisp import PyMISP  # type: ignore

            self._misp = PyMISP(self.url, self.api_key, ssl=False, timeout=10)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("MISP 连接失败, 降级 mock: {}", str(e)[:120])
            self._mock = True  # 降级为 mock
            return True

    async def health_check(self) -> bool:
        return self._mock or self._misp is not None

    async def close(self) -> None:
        self._misp = None

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        """从任务资产派生 IOC 候选, 查询信誉。

        S2 简化: 从 ctx.asset_ips + query_hint.iocs 取 IOC 列表, 逐个查 MISP。
        """
        iocs: list[tuple[str, str]] = []
        for ip in ctx.asset_ips:
            iocs.append(("ip", ip))
        for d in (ctx.query_hint.get("domains") or []):
            iocs.append(("domain", d))
        for h in (ctx.query_hint.get("hashes") or []):
            iocs.append(("hash", h))

        if self._mock or not self._misp:
            return get_mock_evidence("misp", ctx)

        results: list[RawEvidence] = []
        for ioc_type, value in iocs:
            rep = await self._safe_query(ioc_type, value)
            if rep is None:
                continue
            results.append(RawEvidence(
                evidence_type="intel", source="misp", raw=rep, parsed=rep,
                related_assets=[], importance="high" if rep.get("malicious") else "low",
            ))
        return results

    async def _safe_query(self, ioc_type: IOCType, value: str) -> dict[str, Any] | None:
        import asyncio

        def _sync() -> dict[str, Any]:
            type_attr = {"ip": "ip-dst", "domain": "domain", "hash": None}.get(ioc_type)
            if type_attr:
                res = self._misp.search(type_attribute=type_attr, value=value, metadata=True)
            else:
                res = self._misp.search(value=value)
            malicious = bool(res)
            return {
                "ioc_type": ioc_type, "value": value, "malicious": malicious,
                "found": malicious, "events": [e.get("Event", {}).get("info", "") for e in (res or [])],
            }

        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:  # noqa: BLE001
            logger.warning("MISP 查询 {} 失败: {}", value, str(e)[:100])
            return None
