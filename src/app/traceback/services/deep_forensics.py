"""深度取证收集器

对齐 02_phase2_design.md §4.5
- 并行收集 5 个深度适配器(Zeek/Velociraptor/YARA/CAPE/Volatility), 单源超时降级
- CAPE/Volatility 慢源不阻塞: collect 内部带超时, 超时降级 mock
- 产出 deep_evidence_ids (三段式存储), 供归因阶段增量纳入特征
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.app.traceback.adapters.manager import ComponentManager, get_manager
from src.app.traceback.core.config import settings
from src.app.traceback.core.db import session_scope
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.state import TracebackState
from src.app.traceback.repositories import evidence_repo, task_repo
from src.app.traceback.schemas.evidence import CollectContext, RawEvidence

_DEEP_SOURCES = ["zeek", "velociraptor", "yara", "cape", "volatility"]


class DeepForensicsCollector:
    """深度取证收集"""

    def __init__(self, manager: ComponentManager | None = None) -> None:
        self.manager = manager or get_manager()

    async def run(self, state: TracebackState) -> dict[str, Any]:
        task_id = state["task_id"]
        logger.info("[{}] 深度取证开始", task_id)

        async with session_scope() as session:
            task = await task_repo.get_task(session, task_id)
        if not task:
            return {"deep_evidence_ids": []}

        ctx = CollectContext(
            task_id=task_id,
            asset_ips=task.get("asset_ips") or [],
            asset_hosts=task.get("asset_hosts") or [],
        )

        raw_items = await self._collect_parallel(ctx)
        logger.info("[{}] 深度取证收集 {} 条", task_id, len(raw_items))

        deep_ids: list[str] = []
        async with session_scope() as session:
            for item in raw_items:
                eid = await evidence_repo.save_evidence(
                    session,
                    task_id=task_id,
                    evidence_type=item.evidence_type,
                    source=item.source,
                    raw_data=item.raw,
                    parsed_data=item.parsed,
                    importance=item.importance,
                    related_assets=item.related_assets,
                )
                deep_ids.append(eid)
        logger.info("[{}] 深度取证完成, 存证 {} 条", task_id, len(deep_ids))
        return {"deep_evidence_ids": deep_ids}

    async def _collect_parallel(self, ctx: CollectContext) -> list[RawEvidence]:
        async def _one(name: str) -> list[RawEvidence]:
            adapter = self.manager.get(name)
            if adapter is None:
                return []
            try:
                # 慢源(CAPE/Volatility)用更长超时, 但仍不阻塞整体(超时降级)
                timeout = settings.evidence_collect_timeout * (5 if name in ("cape", "volatility") else 1)
                return await asyncio.wait_for(adapter.collect(ctx), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("深度适配器 {} 超时, 降级", name)
                return []
            except Exception as e:  # noqa: BLE001
                logger.warning("深度适配器 {} 异常, 降级: {}", name, str(e)[:100])
                return []

        results = await asyncio.gather(*(_one(n) for n in _DEEP_SOURCES))
        return [item for sub in results for item in sub]
