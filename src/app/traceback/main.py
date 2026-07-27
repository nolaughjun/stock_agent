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
from src.app.traceback.api.v1 import analysis, results, tasks
from src.app.traceback.core.config import settings
from src.app.traceback.core.db import close_all
from src.app.traceback.core.logging import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("启动 {} v{}", settings.app_name, settings.app_version)

    # 初始化适配器 (并行连接+健康检查, 失败降级)
    manager = get_manager()
    register_defaults(manager)
    health = await manager.initialize()
    logger.info("适配器健康状态: {}", health)

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
app.include_router(tasks.router, prefix=API_PREFIX)
app.include_router(analysis.router, prefix=API_PREFIX)
app.include_router(results.router, prefix=API_PREFIX)


@app.get("/health", tags=["系统"])
async def health():
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.app.traceback.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
    )
