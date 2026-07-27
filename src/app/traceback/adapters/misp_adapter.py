"""MISP 威胁情报适配器 (P1 stub)

对齐 traceback_system_prd.md §9.16.2-C — IP/域名/Hash 信誉查询。
M1 stub: 无 misp_url 时降级返回空。
"""
from __future__ import annotations

from typing import Any, Literal

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.core.config import settings

IOCType = Literal["ip", "domain", "hash"]


class MISPAdapter(BaseAdapter):
    name = "misp"

    def __init__(self) -> None:
        self.url = settings.misp_url
        self.api_key = settings.misp_api_key

    async def connect(self) -> bool:
        return bool(self.url)

    async def health_check(self) -> bool:
        return bool(self.url)

    async def close(self) -> None:
        pass

    async def query_reputation(self, ioc_type: IOCType, value: str) -> dict[str, Any]:
        """查询 IOC 信誉 (IP/域名/Hash)"""
        if not self.url:
            return {"ioc_type": ioc_type, "value": value, "malicious": False, "found": False}
        # S2: pymisp 查询, 解析 events/tags/malware_family
        raise NotImplementedError("S2 实现真实 MISP 查询")
