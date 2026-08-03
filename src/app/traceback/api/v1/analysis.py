"""分析控制与审核接口

对齐 01_phase1_design.md §6.2 / §9 时序
- analyze : 启动分析 (经 task_runner 调度; asyncio 默认, celery 可选)
- progress: 查询进度
- review  : 提交人工审核 (自动触发恢复)
- resume  : 恢复被中断的分析
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from src.app.traceback.api.deps import ReviewerDep, SessionDep
from src.app.traceback.orchestration.state import STAGE_PROGRESS, AWAITING_REVIEW_STAGES
from src.app.traceback.repositories import task_repo
from src.app.traceback.schemas.review import ResumeRequest, ReviewRequest
from src.app.traceback.schemas.task import TaskProgress
from src.app.traceback.services import task_runner

router = APIRouter(prefix="/tasks", tags=["分析控制"])


@router.post("/{task_id}/analyze", status_code=status.HTTP_202_ACCEPTED)
async def analyze(task_id: str, session: SessionDep, _: ReviewerDep):
    """开始分析 (入队)"""
    task = await task_repo.get_task(session, task_id)
    if not task:
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    if task_runner.is_running(task_id):
        return {"task_id": task_id, "message": "任务已在分析中"}
    await task_runner.dispatch_analysis(task_id)
    return {"task_id": task_id, "status": "analyzing", "message": "已加入分析队列"}


@router.get("/{task_id}/progress", response_model=TaskProgress)
async def progress(task_id: str, session: SessionDep, _: ReviewerDep):
    task = await task_repo.get_task(session, task_id)
    if not task:
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    stage = task["current_stage"]
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
    """提交人工审核并触发恢复"""
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
    decision = payload.model_dump()
    decision["reviewer"] = reviewer
    await task_runner.dispatch_resume(task_id, decision)
    return {"task_id": task_id, "review_id": review_id, "status": "resuming"}


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
    if task_runner.is_running(task_id):
        return {"task_id": task_id, "message": "任务正在处理中"}
    decision = payload.decision or {"action": "approve", "reviewer": "anonymous"}
    await task_runner.dispatch_resume(task_id, decision)
    return {"task_id": task_id, "status": "resuming"}
