"""Timesketch 适配器 (P1 stub)

对齐 traceback_system_prd.md §9.16.2-A — 时间线可视化(创建sketch/上传事件/返回URL)。
M1 stub, S3 实现真实 REST 对接。
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.core.config import settings


class TimesketchAdapter(BaseAdapter):
    name = "timesketch"

    def __init__(self) -> None:
        self.url = settings.timesketch_url
        self.api_key = settings.timesketch_api_key

    async def connect(self) -> bool:
        return bool(self.url)

    async def health_check(self) -> bool:
        return bool(self.url)

    async def close(self) -> None:
        pass

    async def create_timeline(
        self, name: str, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """创建 sketch + 上传 JSONL 事件, 返回 {sketch_id, sketch_url}"""
        if not self.url:
            return {"sketch_id": None, "sketch_url": None, "events_uploaded": 0}
        # S3: requests POST /api/v1/sketches/, 上传事件, 返回 iframe URL
        raise NotImplementedError("S3 实现真实 Timesketch 对接")
