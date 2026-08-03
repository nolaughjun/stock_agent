"""分析任务调度服务

对齐 01_phase1_design.md §2.1 / §9 时序。
- 当前实现: 进程内 asyncio 后台任务 (单进程 MemorySaver 一致, 已测试)
- 生产路径: Celery + Redis (core/celery_app.py 脚手架, 懒加载)
  注: Celery 多 worker 需 S4 PG checkprinter 替换 MemorySaver, 否则状态不一致。

调度逻辑从 api/v1/analysis.py 抽出, 便于后续替换为 Celery 而不动 API 层。
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.app.traceback.core.db import session_scope
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration import runner
from src.app.traceback.orchestration.state import AWAITING_REVIEW_STAGES
from src.app.traceback.repositories import task_repo

# 运行中的任务协程 (进程内; Celery 化后由 broker 接管)
_running: dict[str, asyncio.Task[Any]] = {}


def is_running(task_id: str) -> bool:
    return task_id in _running


async def dispatch_analysis(task_id: str) -> None:
    """启动分析: 驱动图运行至 interrupt/END, 同步状态到 PG"""
    _running[task_id] = asyncio.create_task(_drive_start(task_id))


async def dispatch_resume(task_id: str, decision: dict[str, Any]) -> None:
    """恢复分析: 携带审核决策恢复运行"""
    _running[task_id] = asyncio.create_task(_drive_resume(task_id, decision))


async def _drive_start(task_id: str) -> None:
    try:
        async with session_scope() as session:
            await task_repo.set_stage(session, task_id, "collecting", status="analyzing")
        state = await runner.run_until_interrupt(
            task_id, initial_state={"task_id": task_id, "stage": "created"}
        )
        async with session_scope() as session:
            await task_repo.set_stage(session, task_id, state.get("stage", "created"))
        logger.info("[{}] 分析驱动结束 stage={}", task_id, state.get("stage"))
    except Exception as e:  # noqa: BLE001
        logger.exception("[{}] 分析驱动异常: {}", task_id, str(e)[:200])
        async with session_scope() as session:
            await task_repo.set_stage(session, task_id, "created", status="failed")
    finally:
        _running.pop(task_id, None)


async def _drive_resume(task_id: str, decision: dict[str, Any]) -> None:
    try:
        state = await runner.resume_with_decision(task_id, decision)
        async with session_scope() as session:
            await task_repo.set_stage(session, task_id, state.get("stage", "created"))
        logger.info("[{}] 恢复后 stage={}", task_id, state.get("stage"))
    except Exception as e:  # noqa: BLE001
        logger.exception("[{}] 恢复驱动异常: {}", task_id, str(e)[:200])
    finally:
        _running.pop(task_id, None)


def awaiting_review(stage: str) -> bool:
    return stage in AWAITING_REVIEW_STAGES
