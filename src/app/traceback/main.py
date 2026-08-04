"""溯源分析系统 FastAPI 入口

对齐 01_phase1_design.md §2.2 / §6
遵循项目既有约定: sys.path 注入 + lifespan (参考 investment_research/main.py)
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.app.traceback.adapters.manager import get_manager
from src.app.traceback.adapters.registry import register_defaults
from src.app.traceback.api.v1 import analysis, auth, knowledge, results, tasks
from src.app.traceback.core.config import settings
from src.app.traceback.core.db import close_all
from src.app.traceback.core.logging import logger
from src.app.traceback.core.metrics import render_metrics
from src.app.traceback.orchestration.checkpointer import init_checkpointer
from src.app.traceback.orchestration.graph import set_checkpointer


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("启动 {} v{}", settings.app_name, settings.app_version)

    # 初始化检查点后端 (memory 默认 / postgres 生产级)
    checkpointer = await init_checkpointer()
    set_checkpointer(checkpointer)

    # 初始化适配器 (并行连接+健康检查, 失败降级)
    manager = get_manager()
    register_defaults(manager)
    health = await manager.initialize()
    logger.info("适配器健康状态: {}", health)

    # P3 知识库种子加载 (库为空时)
    try:
        from src.app.traceback.core.db import session_scope
        from src.app.traceback.repositories import knowledge_repo

        async with session_scope() as session:
            await knowledge_repo.seed_if_empty(session)
    except Exception as e:  # noqa: BLE001
        logger.warning("知识库种子加载跳过: {}", str(e)[:100])

    yield

    await manager.close_all()
    await close_all()
    logger.info("应用已关闭")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="基于 AI 大模型的安全事件溯源分析平台 — P1 MVP",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1/traceback"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(tasks.router, prefix=API_PREFIX)
app.include_router(analysis.router, prefix=API_PREFIX)
app.include_router(results.router, prefix=API_PREFIX)
app.include_router(knowledge.router, prefix=API_PREFIX)


@app.get("/health", tags=["系统"])
async def health():
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@app.get("/metrics", tags=["系统"])
async def metrics():
    """Prometheus 指标 (OBS-002)"""
    from fastapi import Response

    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.app.traceback.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
    )
