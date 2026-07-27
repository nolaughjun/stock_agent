"""EDR 适配器 (P1 stub)

对齐 01_phase1_design.md §7.2 — 终端进程/网络行为查询。
M1 stub, S2 对接真实 EDR REST。
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.core.config import settings


class EDRAdapter(BaseAdapter):
    name = "edr"

    def __init__(self) -> None:
        self.base_url = settings.edr_base_url
        self.api_key = settings.edr_api_key

    async def connect(self) -> bool:
        return bool(self.base_url)

    async def health_check(self) -> bool:
        return bool(self.base_url)

    async def close(self) -> None:
        pass

    async def get_endpoint_behavior(
        self, host: str, start: str, end: str
    ) -> list[dict[str, Any]]:
        if not self.base_url:
            return []
        raise NotImplementedError("S2 实现真实 EDR 查询")
