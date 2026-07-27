"""任务管理接口

对齐 01_phase1_design.md §6.1 (US-001)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from src.app.traceback.api.deps import ReviewerDep, SessionDep
from src.app.traceback.repositories import task_repo
from src.app.traceback.schemas.task import (
    TaskCreate,
    TaskListOut,
    TaskOut,
    TaskUpdate,
)

router = APIRouter(prefix="/tasks", tags=["任务管理"])


def _not_found(task_id: str):
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "TB-1001", "message": f"任务不存在: {task_id}"},
    )


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, session: SessionDep, _: ReviewerDep):
    """创建溯源任务 (US-001)"""
    task_id = await task_repo.create_task(session, payload)
    task = await task_repo.get_task(session, task_id)
    return TaskOut(**task)


@router.get("", response_model=TaskListOut)
async def list_tasks(
    session: SessionDep,
    _: ReviewerDep,
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    items, total = await task_repo.list_tasks(
        session, status=status_filter, page=page, page_size=page_size
    )
    return TaskListOut(total=total, items=[TaskOut(**i) for i in items])


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, session: SessionDep, _: ReviewerDep):
    task = await task_repo.get_task(session, task_id)
    if not task:
        _not_found(task_id)
    return TaskOut(**task)


@router.put("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: str, payload: TaskUpdate, session: SessionDep, _: ReviewerDep
):
    if not await task_repo.get_task(session, task_id):
        _not_found(task_id)
    task = await task_repo.update_task(session, task_id, payload)
    return TaskOut(**task)


@router.delete("/{task_id}")
async def cancel_task(task_id: str, session: SessionDep, _: ReviewerDep):
    if not await task_repo.cancel_task(session, task_id):
        _not_found(task_id)
    return {"task_id": task_id, "status": "cancelled"}
