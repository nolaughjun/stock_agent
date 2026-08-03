"""DNS 适配器 (P1: mock)

对齐 01_phase1_design.md §7.2 — DNS 解析记录查询。
P1 无真实 DNS 服务配置, 返回 mock 解析记录; S3+ 可对接真实 DNS 日志服务。
"""
from __future__ import annotations

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.adapters.mock_data import get_mock_evidence
from src.app.traceback.core.config import settings
from src.app.traceback.schemas.evidence import CollectContext, RawEvidence


class DNSAdapter(BaseAdapter):
    name = "dns"

    def __init__(self) -> None:
        self.base_url = settings.dns_base_url

    async def connect(self) -> bool:
        return True  # mock 模式始终可用; 真实 DNS S3 对接

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        if not self.base_url:
            return get_mock_evidence("dns", ctx)
        raise NotImplementedError("S3 对接真实 DNS 服务")
