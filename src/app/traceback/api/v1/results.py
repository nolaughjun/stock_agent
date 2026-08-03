"""结果查询接口

对齐 01_phase1_design.md §6.3
M1 骨架: 时间线/攻击链/报告查询返回占位结构, S2~S4 接真实存储。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.app.traceback.api.deps import ReviewerDep, SessionDep
from src.app.traceback.repositories import (
    attack_chain_repo,
    attribution_repo,
    evidence_repo,
    task_repo,
    timeline_repo,
)
from src.app.traceback.schemas.attack_chain import AttackChainOut
from src.app.traceback.schemas.attribution import AttributionOut
from src.app.traceback.schemas.evidence import EvidenceOut
from src.app.traceback.schemas.timeline import TimelineOut

router = APIRouter(prefix="/tasks", tags=["结果查询"])


def _ensure_task(session, task_id):
    return task_repo.get_task(session, task_id)


@router.get("/{task_id}/timeline", response_model=TimelineOut)
async def get_timeline(task_id: str, session: SessionDep, _: ReviewerDep):
    if not await _ensure_task(session, task_id):
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    events = await timeline_repo.get_events(task_id)
    key_count = await timeline_repo.get_key_event_count(task_id)
    return TimelineOut(task_id=task_id, events=events, key_event_count=key_count)


@router.get("/{task_id}/attack-chain", response_model=AttackChainOut)
async def get_attack_chain(task_id: str, session: SessionDep, _: ReviewerDep):
    if not await _ensure_task(session, task_id):
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    chain = await attack_chain_repo.get_chain(session, task_id)
    return AttackChainOut(task_id=task_id, chain=chain)


@router.get("/{task_id}/attribution", response_model=AttributionOut)
async def get_attribution(task_id: str, session: SessionDep, _: ReviewerDep):
    """获取归因结果(含候选威胁组织 + 攻击者画像 + 相似案例)"""
    if not await _ensure_task(session, task_id):
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    attr = await attribution_repo.get_attribution(session, task_id)
    return AttributionOut(task_id=task_id, attribution=attr)


@router.get("/{task_id}/report")
async def get_report(
    task_id: str,
    session: SessionDep,
    _: ReviewerDep,
    fmt: str = "pdf",
):
    """下载报告 (pdf/md/docx), 从 MinIO 取"""
    from fastapi import Response

    from src.app.traceback.services import report_service

    if not await _ensure_task(session, task_id):
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    if fmt not in ("pdf", "md", "docx"):
        raise HTTPException(400, detail={"code": "TB-1004", "message": "不支持的格式, 可选 pdf/md/docx"})
    data = await report_service.get_report_from_minio(task_id, fmt)
    if data is None:
        raise HTTPException(404, detail={"code": "TB-1005", "message": f"报告未生成或不支持 {fmt} 格式"})
    media = {"pdf": "application/pdf", "md": "text/markdown",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[fmt]
    return Response(
        content=data, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{task_id}-report.{fmt}"'},
    )


@router.get("/{task_id}/evidences", response_model=list[EvidenceOut])
async def list_evidences(
    task_id: str,
    session: SessionDep,
    _: ReviewerDep,
    evidence_type: str | None = None,
    importance: str | None = None,
):
    """查询任务证据 (合并 PG 引用 + Mongo 元数据)"""
    if not await _ensure_task(session, task_id):
        raise HTTPException(404, detail={"code": "TB-1001", "message": "任务不存在"})
    refs = await evidence_repo.list_evidence(
        session, task_id, evidence_type=evidence_type, importance=importance
    )
    out: list[EvidenceOut] = []
    for ref in refs:
        full = await evidence_repo.get_evidence(session, ref["evidence_id"])
        data = full or ref
        out.append(EvidenceOut(
            evidence_id=data["evidence_id"],
            task_id=data["task_id"],
            evidence_type=data["evidence_type"],
            source=data["source"],
            importance=data["importance"],
            tags=data.get("tags") or [],
            related_assets=data.get("related_assets") or [],
            unavailable=data.get("unavailable", False),
            parsed_data=data.get("parsed_data") or {},
            raw_summary=data.get("raw_summary"),
            collect_time=data["collect_time"],
        ))
    return out
