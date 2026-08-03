"""时间线仓库 — MongoDB

对齐 01_phase1_design.md §5.2 timeline_events collection
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.core.db import get_mongo_db

COLLECTION = "timeline_events"


async def save_events(task_id: str, events: list[dict[str, Any]]) -> int:
    """覆盖式保存任务的时间线事件(先删后插), 返回写入数"""
    db = get_mongo_db()
    await db[COLLECTION].delete_many({"task_id": task_id})
    if events:
        await db[COLLECTION].insert_many(events)
    return len(events)


async def get_events(
    task_id: str, *, key_only: bool = False
) -> list[dict[str, Any]]:
    """读取任务时间线事件(按 event_time 升序)"""
    db = get_mongo_db()
    query: dict[str, Any] = {"task_id": task_id}
    if key_only:
        query["is_key_event"] = True
    cursor = db[COLLECTION].find(query, {"_id": 0}).sort("event_time", 1)
    return await cursor.to_list(length=None)


async def get_key_event_count(task_id: str) -> int:
    return await get_mongo_db()[COLLECTION].count_documents(
        {"task_id": task_id, "is_key_event": True}
    )


async def delete_events(task_id: str) -> int:
    res = await get_mongo_db()[COLLECTION].delete_many({"task_id": task_id})
    return res.deleted_count
