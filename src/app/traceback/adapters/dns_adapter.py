"""DNS 适配器 (P1 stub)

对齐 01_phase1_design.md §7.2 — DNS 解析记录查询。
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.core.config import settings


class DNSAdapter(BaseAdapter):
    name = "dns"

    def __init__(self) -> None:
        self.base_url = settings.dns_base_url

    async def connect(self) -> bool:
        return bool(self.base_url)

    async def health_check(self) -> bool:
        return bool(self.base_url)

    async def close(self) -> None:
        pass

    async def query_resolution(self, domain: str) -> list[dict[str, Any]]:
        if not self.base_url:
            return []
        raise NotImplementedError("S2 实现真实 DNS 查询")
