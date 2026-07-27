"""结果查询接口

对齐 01_phase1_design.md §6.3
M1 骨架: 时间线/攻击链/报告查询返回占位结构, S2~S4 接真实存储。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.app.traceback.api.deps import ReviewerDep, SessionDep
from src.app.traceback.repositories import task_repo
from src.app.traceback.schemas.attack_chain import AttackChainOut
from src.app.traceback.schemas.timeline import TimelineOut

router = APIRouter(prefix="/tasks", tags=["结果查询"])


def _ensure_task(session, task_id):
    return task_repo.get_task(session, task_id)


@router.get("/{task_id}/timeline", response_model=TimelineOut)
async def get_timeline(task_id: str, session: SessionDep, _: ReviewerDep):
    if not await _ensure_task(session, task_id):
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    # S3: 从 MongoDB timeline_events 读取
    return TimelineOut(task_id=task_id, events=[], key_event_count=0)


@router.get("/{task_id}/attack-chain", response_model=AttackChainOut)
async def get_attack_chain(task_id: str, session: SessionDep, _: ReviewerDep):
    if not await _ensure_task(session, task_id):
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    # S3: 从 PG attack_chains.payload 读取
    return AttackChainOut(task_id=task_id, chain=None)


@router.get("/{task_id}/report")
async def get_report(task_id: str, session: SessionDep, _: ReviewerDep):
    if not await _ensure_task(session, task_id):
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    # S4: 从 MinIO 取 PDF/Word/MD 并返回 FileResponse
    return {"task_id": task_id, "message": "报告生成功能 S4 实现", "report_path": None}
