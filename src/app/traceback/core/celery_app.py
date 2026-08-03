"""Celery 应用脚手架 (S4 启用)

对齐 01_phase1_design.md §2.1 Celery Worker + Beat。
当前 S2 用进程内 asyncio 调度 (services/task_runner.py), Celery 懒加载:
  - 需安装 celery 并启动 worker: `celery -A src.app.traceback.core.celery_app worker`
  - 多 worker 需 S4 PG checkprinter 替换 MemorySaver, 否则 interrupt 状态不一致
切换: settings.task_runner = "celery"
"""
from __future__ import annotations

from src.app.traceback.core.config import settings
from src.app.traceback.core.logging import logger

_celery: object | None = None


def get_celery():
    """懒加载 Celery 实例 (未安装 celery 时返回 None)"""
    global _celery
    if _celery is not None:
        return _celery
    try:
        from celery import Celery  # type: ignore

        app = Celery("traceback", broker=settings.redis_url, backend=settings.redis_url)
        app.conf.update(
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
            timezone="Asia/Shanghai",
            enable_utc=True,
            task_track_started=True,
            # 开发模式同步执行 (测试用); 生产关闭
            task_always_eager=settings.debug and settings.task_runner != "celery_prod",
        )

        @app.task(name="traceback.analyze")
        def analyze_task(task_id: str):
            import asyncio

            from src.app.traceback.services import task_runner

            asyncio.run(task_runner._drive_start(task_id))  # noqa: SLF001

        @app.task(name="traceback.resume")
        def resume_task(task_id: str, decision: dict):
            import asyncio

            from src.app.traceback.services import task_runner

            asyncio.run(task_runner._drive_resume(task_id, decision))  # noqa: SLF001

        _celery = app
        logger.info("Celery 应用已初始化 (eager={})", app.conf.task_always_eager)
    except ImportError:
        logger.info("celery 未安装, 使用 asyncio 调度")
        _celery = None
    return _celery
