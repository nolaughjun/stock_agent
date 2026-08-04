"""知识问答 RAG 服务 (P3)

对齐 03_phase3_design.md §4.2
检索案例 + 文章 + IOC → 拼上下文 → LLM 生成回答。
强制附引用, 无相关结果时明确拒答 (防幻觉, RAG 幻觉风险对策)。
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.core.config import settings
from src.app.traceback.core.db import session_scope
from src.app.traceback.core.llm import get_llm
from src.app.traceback.core.logging import logger
from src.app.traceback.repositories import knowledge_repo


async def answer_question(question: str, top_k: int = 5) -> dict[str, Any]:
    """RAG 问答: 检索 → 拼上下文 → LLM 回答。

    Returns: {answer, citations, has_result}
    无相关结果时 has_result=False, 回答明确"知识库无相关记录"。
    """
    # 1. 检索 (案例按关键词 + 文章按关键词 + IOC 直查)
    cases = await _search_cases_by_keyword(question, top_k)
    articles = await knowledge_repo.search_articles(question, top_k=top_k)
    iocs = await _extract_iocs_from_question(question)

    citations: list[dict[str, Any]] = []
    for c in cases:
        citations.append({"type": "case", "id": c.get("case_id"), "title": c.get("title")})
    for a in articles:
        citations.append({"type": "article", "id": a.get("article_id"), "title": a.get("title")})
    for i in iocs:
        if i.get("found"):
            citations.append({"type": "ioc", "id": i.get("ioc_value"),
                              "actor": i.get("threat_actor")})

    if not citations:
        return {
            "answer": "知识库中无与该问题相关的记录。",
            "citations": [], "has_result": False,
        }

    # 2. 拼上下文
    context_parts = []
    for c in cases:
        context_parts.append(f"[案例 {c.get('case_id')}] {c.get('title')}: {c.get('description','')[:200]}")
    for a in articles:
        context_parts.append(f"[文章 {a.get('article_id')}] {a.get('title')}: {a.get('content','')[:300]}")
    for i in iocs:
        if i.get("found"):
            context_parts.append(f"[IOC {i.get('ioc_value')}] 关联组织: {i.get('threat_actor')}")
    context = "\n".join(context_parts)

    # 3. LLM 回答 (强制基于上下文, 引用来源)
    if settings.deepseek_api_key:
        try:
            answer = await get_llm().complete(
                system=(
                    "你是安全知识助手。仅基于下面提供的知识库上下文回答问题。"
                    "回答末尾必须列出引用的来源(case/article/ioc)。"
                    "若上下文不足以回答, 明确说明'知识库无相关记录', 不要编造。"
                ),
                user=f"知识库上下文:\n{context}\n\n问题: {question}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 问答失败, 降级上下文摘要: {}", str(e)[:100])
            answer = _context_summary(question, cases, articles, iocs)
    else:
        answer = _context_summary(question, cases, articles, iocs)

    return {"answer": answer, "citations": citations, "has_result": True}


async def _search_cases_by_keyword(question: str, top_k: int) -> list[dict[str, Any]]:
    """案例关键词检索 (标题/描述/tags), 返回 published 案例"""
    import re as _re

    rx = _re.compile(_re.escape(question.split()[0] if question.split() else question), _re.IGNORECASE)
    cases = await knowledge_repo.list_cases(status="published", limit=100)
    matched = [c for c in cases if rx.search(c.get("title", "")) or rx.search(c.get("description", ""))
               or any(rx.search(t) for t in (c.get("tags") or []))]
    return matched[:top_k] if matched else cases[:top_k]


async def _extract_iocs_from_question(question: str) -> list[dict[str, Any]]:
    """从问题中提取 IP/域名候选, 查 IOC 库"""
    import re as _re

    candidates: list[dict[str, str]] = []
    for ip in _re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", question):
        candidates.append({"type": "ip", "value": ip})
    for dom in _re.findall(r"\b[a-z0-9.-]+\.[a-z]{2,}\b", question, _re.IGNORECASE):
        candidates.append({"type": "domain", "value": dom})
    if not candidates:
        return []
    async with session_scope() as session:
        return await knowledge_repo.batch_query_iocs(session, candidates)


def _context_summary(question, cases, articles, iocs) -> str:
    """无 LLM 时的上下文摘要回答"""
    lines = [f"关于「{question}」, 知识库找到以下相关内容:"]
    for c in cases[:3]:
        lines.append(f"- 案例 {c.get('case_id')}: {c.get('title')}")
    for a in articles[:3]:
        lines.append(f"- 文章 {a.get('article_id')}: {a.get('title')}")
    for i in iocs:
        if i.get("found"):
            lines.append(f"- IOC {i.get('ioc_value')} 关联 {i.get('threat_actor')}")
    lines.append("\n(详细分析需配置 LLM; 以上为知识库检索结果摘要)")
    return "\n".join(lines)
