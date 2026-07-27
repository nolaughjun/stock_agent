"""任务仓库 — PG 数据访问

对齐 01_phase1_design.md §5.1 traceback_tasks / evidences / human_reviews
使用 SQLAlchemy core (table 反射) + async session, 避免 ORM 模型与 schema 重复维护。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import and_, desc, select, table, column
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.traceback.schemas.task import TaskCreate, TaskStatus, TaskUpdate

# 反射表定义 (与 init.sql 对齐)
tasks_tbl = table(
    "traceback_tasks",
    column("task_id"), column("task_name"), column("description"),
    column("status"), column("current_stage"),
    column("alert_ids"), column("asset_ips"), column("asset_hosts"), column("asset_users"),
    column("event_start_time"), column("event_end_time"),
    column("assignee"), column("retry_count"), column("error_message"),
    column("create_time"), column("update_time"),
)
reviews_tbl = table(
    "human_reviews",
    column("review_id"), column("task_id"), column("stage"), column("action"),
    column("reviewer"), column("comments"), column("modifications"), column("review_time"),
)


def new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:12]}"


async def create_task(session: AsyncSession, payload: TaskCreate) -> str:
    task_id = new_task_id()
    await session.execute(
        tasks_tbl.insert().values(
            task_id=task_id,
            task_name=payload.task_name,
            description=payload.description,
            status="created",
            current_stage="created",
            alert_ids=payload.alert_ids,
            asset_ips=payload.asset_ips,
            asset_hosts=payload.asset_hosts,
            asset_users=payload.asset_users,
            event_start_time=payload.event_start_time,
            event_end_time=payload.event_end_time,
            assignee=payload.assignee,
        )
    )
    await session.commit()
    return task_id


async def get_task(session: AsyncSession, task_id: str) -> Optional[dict[str, Any]]:
    row = (await session.execute(
        select(tasks_tbl).where(tasks_tbl.c.task_id == task_id)
    )).mappings().first()
    return dict(row) if row else None


async def list_tasks(
    session: AsyncSession,
    *,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    cond = tasks_tbl.c.task_id.isnot(None)
    if status:
        cond = and_(cond, tasks_tbl.c.status == status)
    total = (await session.execute(
        select(tasks_tbl.c.task_id).where(cond)
    )).all()
    total_n = len(total)
    rows = (await session.execute(
        select(tasks_tbl).where(cond).order_by(desc(tasks_tbl.c.update_time))
        .offset((page - 1) * page_size).limit(page_size)
    )).mappings().all()
    return [dict(r) for r in rows], total_n


async def update_task(
    session: AsyncSession, task_id: str, payload: TaskUpdate
) -> Optional[dict[str, Any]]:
    values = payload.model_dump(exclude_none=True)
    if not values:
        return await get_task(session, task_id)
    await session.execute(
        tasks_tbl.update().where(tasks_tbl.c.task_id == task_id).values(**values)
    )
    await session.commit()
    return await get_task(session, task_id)


async def set_stage(
    session: AsyncSession, task_id: str, stage: str, status: Optional[TaskStatus] = None
) -> None:
    """编排运行器同步业务状态到 PG (进度可查询/可恢复)"""
    values: dict[str, Any] = {"current_stage": stage}
    if status:
        values["status"] = status
    if stage == "reported":
        values["status"] = "done"
    elif stage in ("review_timeline", "review_chain"):
        values["status"] = "reviewing"
    elif stage in ("collecting", "timeline_built", "chain_analyzed", "attributed"):
        values["status"] = "analyzing"
    await session.execute(
        tasks_tbl.update().where(tasks_tbl.c.task_id == task_id).values(**values)
    )
    await session.commit()


async def cancel_task(session: AsyncSession, task_id: str) -> bool:
    existing = await get_task(session, task_id)
    if not existing:
        return False
    await session.execute(
        tasks_tbl.update().where(tasks_tbl.c.task_id == task_id).values(status="failed")
    )
    await session.commit()
    return True


async def add_review(
    session: AsyncSession,
    task_id: str,
    stage: str,
    action: str,
    reviewer: str,
    comments: Optional[str],
    modifications: list[dict[str, Any]],
) -> str:
    review_id = f"rev-{uuid.uuid4().hex[:12]}"
    await session.execute(
        reviews_tbl.insert().values(
            review_id=review_id, task_id=task_id, stage=stage, action=action,
            reviewer=reviewer, comments=comments, modifications=modifications,
        )
    )
    await session.commit()
    return review_id
