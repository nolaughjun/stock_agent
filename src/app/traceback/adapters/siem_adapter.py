"""SIEM 适配器 (P1 stub)

对齐 01_phase1_design.md §7.2 — P1 按 asset+时间查日志。
M1 提供 stub: 无 siem_base_url 时返回空结果并标记降级, S2 对接真实 OpenSearch/Wazuh。
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.core.config import settings


class SIEMAdapter(BaseAdapter):
    name = "siem"

    def __init__(self) -> None:
        self.base_url = settings.siem_base_url
        self.api_key = settings.siem_api_key

    async def connect(self) -> bool:
        return bool(self.base_url)

    async def health_check(self) -> bool:
        return bool(self.base_url)

    async def close(self) -> None:
        pass

    async def query_logs(
        self, asset: str, start: str, end: str, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """按资产+时间范围查询 SIEM 日志"""
        if not self.base_url:
            return []  # 降级: 返回空
        # S2: 用 httpx 调 OpenSearch _search (DSL), 经白名单字段校验防注入
        raise NotImplementedError("S2 实现真实 SIEM 查询")
