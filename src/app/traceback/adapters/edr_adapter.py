"""EDR 适配器 (P1: mock)

对齐 01_phase1_design.md §7.2 — 终端进程/网络行为查询。
P1 无真实 EDR 配置, 返回 mock 终端行为数据 (TR-002 对策); S3+ 可对接 Velociraptor。
"""
from __future__ import annotations

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.adapters.mock_data import get_mock_evidence
from src.app.traceback.core.config import settings
from src.app.traceback.schemas.evidence import CollectContext, RawEvidence


class EDRAdapter(BaseAdapter):
    name = "edr"

    def __init__(self) -> None:
        self.base_url = settings.edr_base_url

    async def connect(self) -> bool:
        return True  # mock 模式始终可用; 真实 EDR S3 对接

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        # P1 无真实 EDR; 配置了 base_url 则 S3 对接, 否则 mock
        if not self.base_url:
            return get_mock_evidence("edr", ctx)
        raise NotImplementedError("S3 对接 Velociraptor/真实 EDR")
