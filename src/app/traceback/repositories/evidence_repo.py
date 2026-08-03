"""证据三段式存储仓库

对齐 01_phase1_design.md §5 / §4.1 证据存储分离:
  - 原始大字段 → MinIO (evidence-raw bucket)
  - 结构化元数据 → MongoDB (evidence_meta collection)
  - 轻量引用 → PostgreSQL (evidences 表)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import desc, select, table, column
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.traceback.core.db import get_minio_client, get_mongo_db
from src.app.traceback.core.logging import logger
from src.app.traceback.schemas.evidence import EvidenceMeta, EvidenceRef, ImportanceLevel

# PG evidences 表反射
_evidences_tbl = table(
    "evidences",
    column("evidence_id"), column("task_id"), column("evidence_type"),
    column("source"), column("importance"), column("tags"),
    column("object_key"), column("meta_id"), column("collect_time"),
)

COLLECTION = "evidence_meta"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _object_key(task_id: str, evidence_id: str) -> str:
    return f"{task_id}/{evidence_id}.json"


async def save_evidence(
    session: AsyncSession,
    *,
    task_id: str,
    evidence_type: str,
    source: str,
    raw_data: Any,
    parsed_data: dict[str, Any] | None = None,
    importance: ImportanceLevel = "medium",
    tags: list[str] | None = None,
    related_assets: list[str] | None = None,
    unavailable: bool = False,
    bucket: str = "evidence-raw",
) -> str:
    """三段式保存证据, 返回 evidence_id。

    raw_data 会被序列化为 JSON 存入 MinIO; parsed_data 入 Mongo; 引用入 PG。
    """
    evidence_id = _new_id("evd")
    meta_id = _new_id("meta")
    object_key = _object_key(task_id, evidence_id)
    now = datetime.now()
    tags = tags or []
    related_assets = related_assets or []
    parsed_data = parsed_data or {}

    # 1. MinIO 原始数据 (同步 SDK, 放线程池)
    raw_bytes = json.dumps(raw_data, ensure_ascii=False, default=str).encode("utf-8")
    try:
        await _put_object(bucket, object_key, raw_bytes)
    except Exception as e:  # noqa: BLE001
        logger.warning("MinIO 写入失败, 证据 {} 降级(仅元数据): {}", evidence_id, str(e)[:120])
        object_key = ""  # 标记无原始对象

    # 2. MongoDB 元数据
    meta = EvidenceMeta(
        evidence_id=evidence_id, task_id=task_id, evidence_type=evidence_type,
        source=source, collect_time=now, parsed_data=parsed_data,
        raw_summary=_summarize(raw_data), related_assets=related_assets,
        importance=importance, tags=tags, unavailable=unavailable,
    )
    mongo_db = get_mongo_db()
    await mongo_db[COLLECTION].insert_one({"_id": meta_id, **meta.model_dump()})

    # 3. PG 引用
    await session.execute(
        _evidences_tbl.insert().values(
            evidence_id=evidence_id, task_id=task_id, evidence_type=evidence_type,
            source=source, importance=importance, tags=tags,
            object_key=object_key, meta_id=meta_id, collect_time=now,
        )
    )
    await session.commit()
    return evidence_id


async def get_evidence(
    session: AsyncSession, evidence_id: str
) -> Optional[dict[str, Any]]:
    """合并 PG 引用 + Mongo 元数据"""
    row = (await session.execute(
        select(_evidences_tbl).where(_evidences_tbl.c.evidence_id == evidence_id)
    )).mappings().first()
    if not row:
        return None
    meta = await get_mongo_db()[COLLECTION].find_one({"evidence_id": evidence_id})
    meta.pop("_id", None) if meta else None
    return {**dict(row), **(meta or {})}


async def list_evidence(
    session: AsyncSession,
    task_id: str,
    *,
    evidence_type: Optional[str] = None,
    importance: Optional[str] = None,
) -> list[dict[str, Any]]:
    cond = _evidences_tbl.c.task_id == task_id
    if evidence_type:
        cond = cond & (_evidences_tbl.c.evidence_type == evidence_type)
    if importance:
        cond = cond & (_evidences_tbl.c.importance == importance)
    rows = (await session.execute(
        select(_evidences_tbl).where(cond).order_by(desc(_evidences_tbl.c.collect_time))
    )).mappings().all()
    return [dict(r) for r in rows]


async def delete_evidence(session: AsyncSession, evidence_id: str) -> bool:
    row = (await session.execute(
        select(_evidences_tbl.c.meta_id, _evidences_tbl.c.object_key)
        .where(_evidences_tbl.c.evidence_id == evidence_id)
    )).mappings().first()
    if not row:
        return False
    # 删 Mongo + MinIO + PG
    await get_mongo_db()[COLLECTION].delete_one({"evidence_id": evidence_id})
    if row["object_key"]:
        await _safe_remove_object("evidence-raw", row["object_key"])
    await session.execute(
        _evidences_tbl.delete().where(_evidences_tbl.c.evidence_id == evidence_id)
    )
    await session.commit()
    return True


# ---- MinIO 辅助 (同步 SDK 包线程池) ----

async def _put_object(bucket: str, key: str, data: bytes) -> None:
    client = get_minio_client()
    await asyncio.to_thread(_sync_put, client, bucket, key, data)


def _sync_put(client, bucket: str, key: str, data: bytes) -> None:
    from io import BytesIO

    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    client.put_object(bucket, key, BytesIO(data), length=len(data),
                      content_type="application/json")


async def _safe_remove_object(bucket: str, key: str) -> None:
    try:
        client = get_minio_client()
        await asyncio.to_thread(client.remove_object, bucket, key)
    except Exception as e:  # noqa: BLE001
        logger.debug("MinIO 删除对象失败 {}: {}", key, str(e)[:80])


def _summarize(raw_data: Any, max_len: int = 500) -> str:
    """原始数据文本摘要(存 Mongo, 避免大字段)"""
    try:
        text = json.dumps(raw_data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(raw_data)
    return text[:max_len] + ("..." if len(text) > max_len else "")
