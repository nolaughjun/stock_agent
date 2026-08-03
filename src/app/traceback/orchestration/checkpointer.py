"""LangGraph 检查点后端工厂

对齐 01_phase1_design.md §5.1 / §3.4 — 任务状态持久化, 进程重启可恢复中断。
- memory (默认): MemorySaver, 进程内, 无外部依赖
- postgres: AsyncPostgresSaver (langgraph-checkpoint-postgres), 中断可跨进程恢复,
  为 Celery 多 worker 铺路 (§5.1 langgraph_checkpoints 设计目标)

postgres 后端需 PG 可达 + langgraph-checkpoint-postgres 已安装, 否则自动降级 memory。
"""
from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from src.app.traceback.core.config import settings
from src.app.traceback.core.logging import logger


async def make_pg_saver() -> Any | None:
    """创建 PG AsyncPostgresSaver, 失败返回 None (降级 memory)"""
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # type: ignore
        from psycopg_pool import AsyncConnectionPool  # type: ignore

        conninfo = settings.postgres_dsn.replace("+asyncpg", "").replace(
            "postgresql://", "postgresql://"
        )
        pool = AsyncConnectionPool(
            conninfo=conninfo, max_size=10, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.open()
        saver = AsyncPostgresSaver(conn=pool)
        await saver.setup()  # 建检查点表
        logger.info("PG 检查点后端已启用 (AsyncPostgresSaver)")
        return saver
    except Exception as e:  # noqa: BLE001
        logger.warning("PG 检查点后端不可用, 降级 memory: {}", str(e)[:120])
        return None


async def init_checkpointer() -> Any:
    """按配置初始化检查点后端 (供 app lifespan 调用)"""
    if settings.checkpoint_backend == "postgres":
        saver = await make_pg_saver()
        if saver is not None:
            return saver
    return MemorySaver()
