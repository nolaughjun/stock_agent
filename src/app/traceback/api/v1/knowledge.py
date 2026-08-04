"""知识管理 API (P3)

对齐 traceback_system_prd.md §7.1.4 / 03_phase3_design.md §8
案例 / IOC / 处置方案 / 知识文章 / 知识问答(RAG)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.app.traceback.api.deps import ReviewerDep, SessionDep
from src.app.traceback.repositories import knowledge_repo
from src.app.traceback.services import qa

router = APIRouter(prefix="/knowledge", tags=["知识库"])


# ---------- 案例管理 ----------

class CaseCreate(BaseModel):
    source_task_id: str | None = None
    title: str
    description: str = ""
    attack_type: str = "other"
    tags: list[str] = Field(default_factory=list)
    ttps: list[str] = Field(default_factory=list)
    iocs: list[dict[str, Any]] = Field(default_factory=list)


class SimilarSearch(BaseModel):
    attack_type: str = ""
    ttps: list[str] = Field(default_factory=list)
    iocs: list[str] = Field(default_factory=list)
    top_k: int = 3


@router.post("/cases")
async def create_case(payload: CaseCreate, _: ReviewerDep):
    import uuid

    case_id = f"case-{uuid.uuid4().hex[:10]}"
    case = {**payload.model_dump(), "case_id": case_id, "status": "draft"}
    await knowledge_repo.save_case(case)
    return {"case_id": case_id, "status": "draft"}


@router.get("/cases")
async def list_cases(
    _: ReviewerDep,
    attack_type: str | None = Query(None),
    tags: str | None = Query(None, description="逗号分隔"),
):
    tag_list = tags.split(",") if tags else None
    cases = await knowledge_repo.list_cases(attack_type=attack_type, tags=tag_list)
    return {"total": len(cases), "items": cases}


@router.get("/cases/{case_id}")
async def get_case(case_id: str, _: ReviewerDep):
    case = await knowledge_repo.get_case(case_id)
    if not case:
        raise HTTPException(404, detail={"code": "TB-2001", "message": "案例不存在"})
    return case


@router.post("/cases/{case_id}/publish")
async def publish_case(case_id: str, _: ReviewerDep):
    case = await knowledge_repo.get_case(case_id)
    if not case:
        raise HTTPException(404, detail={"code": "TB-2001", "message": "案例不存在"})
    case["status"] = "published"
    await knowledge_repo.save_case(case)
    return {"case_id": case_id, "status": "published"}


@router.post("/cases/search-similar")
async def search_similar(payload: SimilarSearch, _: ReviewerDep):
    """相似案例检索 (Top-K)"""
    results = await knowledge_repo.search_similar_cases(
        {"attack_type": payload.attack_type, "ttps": payload.ttps,
         "iocs": payload.iocs},
        top_k=payload.top_k,
    )
    return {"cases": results}


# ---------- IOC 知识库 ----------

class IOCCreate(BaseModel):
    ioc_type: str
    ioc_value: str
    threat_type: str = ""
    malware_family: str | None = None
    threat_actor: str | None = None
    source_case_id: str | None = None


class IOCBatchQuery(BaseModel):
    iocs: list[dict[str, str]]


@router.get("/iocs")
async def query_ioc(
    session: SessionDep, _: ReviewerDep,
    ioc_type: str = Query(...), value: str = Query(...),
):
    results = await knowledge_repo.batch_query_iocs(
        session, [{"type": ioc_type, "value": value}]
    )
    return results[0] if results else {"found": False}


@router.post("/iocs/batch-query")
async def batch_query_iocs(payload: IOCBatchQuery, session: SessionDep, _: ReviewerDep):
    results = await knowledge_repo.batch_query_iocs(session, payload.iocs)
    return {"results": results}


@router.post("/iocs")
async def add_ioc(payload: IOCCreate, session: SessionDep, _: ReviewerDep):
    ioc_id = await knowledge_repo.save_ioc(session, payload.model_dump())
    return {"ioc_id": ioc_id}


# ---------- 处置方案 ----------

class PlaybookRecommendReq(BaseModel):
    attack_type: str
    ttps: list[str] = Field(default_factory=list)
    top_k: int = 3


@router.get("/playbooks")
async def list_playbooks(
    session: SessionDep, _: ReviewerDep, attack_type: str | None = Query(None)
):
    pbs = await knowledge_repo.list_playbooks(session, attack_type=attack_type)
    return {"total": len(pbs), "items": pbs}


@router.get("/playbooks/{playbook_id}")
async def get_playbook(playbook_id: str, session: SessionDep, _: ReviewerDep):
    pbs = await knowledge_repo.list_playbooks(session)
    pb = next((p for p in pbs if p["playbook_id"] == playbook_id), None)
    if not pb:
        raise HTTPException(404, detail={"code": "TB-2002", "message": "处置方案不存在"})
    return pb


@router.post("/playbooks/recommend")
async def recommend_playbooks(payload: PlaybookRecommendReq, session: SessionDep, _: ReviewerDep):
    """处置方案推荐 (attack_type + TTP 重叠排序)"""
    results = await knowledge_repo.recommend_playbooks(
        session, payload.attack_type, payload.ttps, top_k=payload.top_k
    )
    return {"recommended_playbooks": results}


# ---------- 知识文章与问答 ----------

class ArticleCreate(BaseModel):
    title: str
    content: str = ""
    category: str = "methodology"
    tags: list[str] = Field(default_factory=list)


class ArticleSearch(BaseModel):
    query: str
    top_k: int = 5


class QARequest(BaseModel):
    question: str


@router.post("/articles")
async def create_article(payload: ArticleCreate, _: ReviewerDep):
    import uuid

    article_id = f"art-{uuid.uuid4().hex[:10]}"
    article = {**payload.model_dump(), "article_id": article_id, "status": "published"}
    await knowledge_repo.save_article(article)
    return {"article_id": article_id}


@router.get("/articles")
async def list_articles(_: ReviewerDep, category: str | None = Query(None)):
    articles = await knowledge_repo.list_articles(category=category)
    return {"total": len(articles), "items": articles}


@router.post("/articles/search")
async def search_articles(payload: ArticleSearch, _: ReviewerDep):
    results = await knowledge_repo.search_articles(payload.query, top_k=payload.top_k)
    return {"results": results}


@router.post("/qa")
async def knowledge_qa(payload: QARequest, _: ReviewerDep):
    """知识问答 (RAG, 强制引用 + 无结果拒答)"""
    result = await qa.answer_question(payload.question)
    return result
