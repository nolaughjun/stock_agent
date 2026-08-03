"""攻击链仓库 — PostgreSQL JSONB

对齐 01_phase1_design.md §5.1 attack_chains 表 (每任务一条)
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, table, column
from sqlalchemy.ext.asyncio import AsyncSession

_chains_tbl = table(
    "attack_chains",
    column("chain_id"), column("task_id"), column("payload"), column("create_time"),
)


async def save_chain(session: AsyncSession, task_id: str, payload: dict[str, Any]) -> str:
    """覆盖式保存攻击链(upsert), 返回 chain_id"""
    existing = (await session.execute(
        select(_chains_tbl.c.chain_id).where(_chains_tbl.c.task_id == task_id)
    )).scalar_one_or_none()
    if existing:
        await session.execute(
            _chains_tbl.update()
            .where(_chains_tbl.c.task_id == task_id)
            .values(payload=payload)
        )
        chain_id = existing
    else:
        chain_id = f"chain-{uuid.uuid4().hex[:12]}"
        await session.execute(
            _chains_tbl.insert().values(chain_id=chain_id, task_id=task_id, payload=payload)
        )
    await session.commit()
    return chain_id


async def get_chain(session: AsyncSession, task_id: str) -> dict[str, Any] | None:
    row = (await session.execute(
        select(_chains_tbl.c.payload).where(_chains_tbl.c.task_id == task_id)
    )).scalar_one_or_none()
    return row if isinstance(row, dict) else (None if row is None else dict(row))
