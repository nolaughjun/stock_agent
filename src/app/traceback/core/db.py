"""数据访问层连接管理

对齐 01_phase1_design.md §5 三段式存储
- PostgreSQL: asyncpg (经 SQLAlchemy async) — 任务/证据引用/攻击链/归因/审核/checkpoint
- MongoDB   : motor (async) — 证据元数据/时间线事件
- MinIO     : minio SDK — 证据原始文件/报告
- Redis     : redis.asyncio — Celery 队列 + 缓存

所有连接懒加载, 单例; 测试可用 testcontainers 或注入替换。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Optional

from src.app.traceback.core.config import settings
from src.app.traceback.core.logging import logger

# ---- PostgreSQL (SQLAlchemy async engine) ----
_engine: Optional[Any] = None
_session_factory: Optional[Any] = None


def get_pg_engine():
    """懒加载 PG async engine"""
    global _engine
    if _engine is None:
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker,
            create_async_engine,
        )

        _engine = create_async_engine(
            settings.postgres_dsn,
            echo=settings.debug,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def get_session():
    """PG 异步会话上下文依赖 (FastAPI Depends)"""
    if _session_factory is None:
        get_pg_engine()
    async with _session_factory() as session:  # type: ignore[union-attr]
        yield session


@asynccontextmanager
async def session_scope():
    """PG 异步会话上下文管理器 (供智能体/服务在请求外使用)"""
    if _session_factory is None:
        get_pg_engine()
    async with _session_factory() as session:  # type: ignore[union-attr]
        yield session


# ---- MongoDB (motor) ----
_mongo_client: Optional[Any] = None


def get_mongo_client():
    global _mongo_client
    if _mongo_client is None:
        from motor.motor_asyncio import AsyncIOMotorClient

        _mongo_client = AsyncIOMotorClient(settings.mongo_dsn)
        logger.info("MongoDB client 已建立")
    return _mongo_client


def get_mongo_db():
    return get_mongo_client()[settings.mongo_db]


# ---- MinIO ----
_minio_client: Optional[Any] = None


def get_minio_client():
    global _minio_client
    if _minio_client is None:
        from minio import Minio

        _minio_client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
    return _minio_client


# ---- Redis ----
_redis_client: Optional[Any] = None


def get_redis():
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(
            settings.redis_url, decode_responses=True
        )
    return _redis_client


async def close_all() -> None:
    """应用关闭时释放连接"""
    global _engine, _session_factory, _mongo_client, _minio_client, _redis_client
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
    if _mongo_client is not None:
        _mongo_client.close()
        _mongo_client = None
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
    _minio_client = None
    logger.info("所有数据连接已释放")
