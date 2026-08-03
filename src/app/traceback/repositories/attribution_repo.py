"""归因结果仓库 — PostgreSQL

对齐 01_phase1_design.md §5.1 attributions 表 (每任务一条)
payload 存完整 AttributionResult(含 candidates/actor_profile),
attributed/confidence 冗余字段便于查询。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, table, column
from sqlalchemy.ext.asyncio import AsyncSession

_attr_tbl = table(
    "attributions",
    column("attribution_id"), column("task_id"), column("attributed"),
    column("confidence"), column("matched_iocs"), column("payload"), column("create_time"),
)


async def save_attribution(
    session: AsyncSession, task_id: str, payload: dict[str, Any]
) -> str:
    """覆盖式保存归因结果(upsert), 同步冗余字段"""
    existing = (await session.execute(
        select(_attr_tbl.c.attribution_id).where(_attr_tbl.c.task_id == task_id)
    )).scalar_one_or_none()
    attributed = bool(payload.get("attributed"))
    confidence = float(payload.get("confidence_score") or 0.0)
    matched_iocs = payload.get("matched_iocs") or []
    values = {
        "attributed": attributed, "confidence": confidence,
        "matched_iocs": matched_iocs, "payload": payload,
    }
    if existing:
        await session.execute(
            _attr_tbl.update().where(_attr_tbl.c.task_id == task_id).values(**values)
        )
        aid = existing
    else:
        aid = payload.get("attribution_id") or f"attr-{uuid.uuid4().hex[:10]}"
        await session.execute(
            _attr_tbl.insert().values(
                attribution_id=aid, task_id=task_id, **values
            )
        )
    await session.commit()
    return aid


async def get_attribution(session: AsyncSession, task_id: str) -> dict[str, Any] | None:
    row = (await session.execute(
        select(_attr_tbl.c.payload).where(_attr_tbl.c.task_id == task_id)
    )).scalar_one_or_none()
    if row is None:
        return None
    return row if isinstance(row, dict) else dict(row)
