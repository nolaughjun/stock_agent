"""分析控制与审核接口

对齐 01_phase1_design.md §6.2 / §9 时序
- analyze : 启动分析 (M1 用 asyncio 后台任务驱动图, S2 切 Celery)
- progress: 查询进度
- review  : 提交人工审核
- resume  : 恢复被中断的分析
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, status

from src.app.traceback.api.deps import ReviewerDep, SessionDep
from src.app.traceback.core.db import get_session
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration import runner
from src.app.traceback.orchestration.state import AWAITING_REVIEW_STAGES
from src.app.traceback.repositories import task_repo
from src.app.traceback.schemas.review import ResumeRequest, ReviewRequest
from src.app.traceback.schemas.task import TaskProgress

router = APIRouter(prefix="/tasks", tags=["分析控制"])

# 运行中的任务协程 (M1 进程内; S2 替换为 Celery)
_running: dict[str, asyncio.Task[Any]] = {}


async def _drive(task_id: str) -> None:
    """后台驱动图运行至 interrupt/END, 同步状态到 PG"""
    try:
        async for session in get_session():
            await task_repo.set_stage(session, task_id, "collecting", status="analyzing")
            break
        state = await runner.run_until_interrupt(task_id, initial_state={
            "task_id": task_id, "stage": "created",
        })
        stage = state.get("stage", "created")
        async for session in get_session():
            await task_repo.set_stage(session, task_id, stage)
            break
        logger.info("[{}] 驱动结束 stage={}", task_id, stage)
    except Exception as e:  # noqa: BLE001
        logger.exception("[{}] 分析驱动异常: {}", task_id, str(e)[:200])
        async for session in get_session():
            await task_repo.set_stage(
                session, task_id, "created", status="failed"
            )
            break
    finally:
        _running.pop(task_id, None)


@router.post("/{task_id}/analyze", status_code=status.HTTP_202_ACCEPTED)
async def analyze(task_id: str, session: SessionDep, _: ReviewerDep):
    """开始分析 (入队)"""
    task = await task_repo.get_task(session, task_id)
    if not task:
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    if task_id in _running:
        return {"task_id": task_id, "message": "任务已在分析中"}
    _running[task_id] = asyncio.create_task(_drive(task_id))
    return {"task_id": task_id, "status": "analyzing", "message": "已加入分析队列"}


@router.get("/{task_id}/progress", response_model=TaskProgress)
async def progress(task_id: str, session: SessionDep, _: ReviewerDep):
    task = await task_repo.get_task(session, task_id)
    if not task:
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    stage = task["current_stage"]
    from src.app.traceback.orchestration.state import STAGE_PROGRESS
    review_stage = None
    if stage == "review_timeline":
        review_stage = "timeline"
    elif stage == "review_chain":
        review_stage = "attack_chain"
    return TaskProgress(
        task_id=task_id,
        status=task["status"],
        current_stage=stage,
        progress_percent=STAGE_PROGRESS.get(stage, 0),
        awaiting_review=stage in AWAITING_REVIEW_STAGES,
        review_stage=review_stage,
    )


@router.post("/{task_id}/review")
async def review(
    task_id: str, payload: ReviewRequest, session: SessionDep, reviewer: ReviewerDep
):
    """提交人工审核 (写入 human_reviews, 不直接恢复; 配合 /resume)"""
    task = await task_repo.get_task(session, task_id)
    if not task:
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    expected_stage = {
        "timeline": "review_timeline",
        "attack_chain": "review_chain",
    }.get(payload.stage)
    if expected_stage and task["current_stage"] != expected_stage:
        raise HTTPException(
            400,
            detail={
                "code": "TB-1002",
                "message": f"当前阶段 {task['current_stage']} 不支持 {payload.stage} 审核",
            },
        )
    review_id = await task_repo.add_review(
        session, task_id, payload.stage, payload.action,
        payload.reviewer, payload.comments, payload.modifications,
    )
    # 携带决策恢复
    decision = payload.model_dump()
    decision["reviewer"] = reviewer
    _running[task_id] = asyncio.create_task(_resume(task_id, decision))
    return {"task_id": task_id, "review_id": review_id, "status": "resuming"}


async def _resume(task_id: str, decision: dict[str, Any]) -> None:
    try:
        state = await runner.resume_with_decision(task_id, decision)
        stage = state.get("stage", "created")
        async for session in get_session():
            await task_repo.set_stage(session, task_id, stage)
            break
        logger.info("[{}] 恢复后 stage={}", task_id, stage)
    except Exception as e:  # noqa: BLE001
        logger.exception("[{}] 恢复驱动异常: {}", task_id, str(e)[:200])
    finally:
        _running.pop(task_id, None)


@router.post("/{task_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume(
    task_id: str, payload: ResumeRequest, session: SessionDep, _: ReviewerDep
):
    """恢复被中断的分析 (若无 decision, 用最近一次 /review 结果)"""
    task = await task_repo.get_task(session, task_id)
    if not task:
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    if task["current_stage"] not in AWAITING_REVIEW_STAGES:
        raise HTTPException(
            400,
            detail={"code": "TB-1002", "message": "当前阶段无需恢复/不在审核等待"},
        )
    if task_id in _running:
        return {"task_id": task_id, "message": "任务正在处理中"}
    decision = payload.decision or {"action": "approve", "reviewer": "anonymous"}
    _running[task_id] = asyncio.create_task(_resume(task_id, decision))
    return {"task_id": task_id, "status": "resuming"}
