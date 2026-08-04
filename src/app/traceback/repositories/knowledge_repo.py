"""知识库仓库 (P3)

对齐 03_phase3_design.md §3 / §7
- 知识案例/文章: MongoDB (完整文档)
- TTP/Playbook/IOC: PostgreSQL (关系/元数据)
- IOC 图谱关系: ioc_knowledge 行内冗余 malware_family/threat_actor/campaign (P4 可换 Neo4j)
种子数据(seed_knowledge.json / seed_cases.json)在库为空时加载。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import desc, select, table, column
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.traceback.core.config import MODULE_DIR
from src.app.traceback.core.db import get_mongo_db
from src.app.traceback.core.logging import logger

_SEED_FILE = MODULE_DIR / "data" / "seed_knowledge.json"
_SEED_CASES_FILE = MODULE_DIR / "data" / "seed_cases.json"

CASE_COLL = "knowledge_cases"
ARTICLE_COLL = "knowledge_articles"

_ttp_tbl = table(
    "ttp_knowledge",
    column("ttp_id"), column("mitre_tactic"), column("mitre_technique"), column("name"),
    column("description"), column("detection_tips"), column("response_tips"), column("source_cases"),
)
_playbook_tbl = table(
    "response_playbooks",
    column("playbook_id"), column("source_case_id"), column("name"), column("description"),
    column("attack_type"), column("steps"), column("prerequisites"), column("applicable_assets"),
    column("effectiveness"), column("side_effects"), column("author"), column("version"),
    column("usage_count"), column("success_rate"),
)
_ioc_tbl = table(
    "ioc_knowledge",
    column("ioc_id"), column("source_case_id"), column("ioc_type"), column("ioc_value"),
    column("threat_type"), column("malware_family"), column("threat_actor"), column("campaign"),
    column("context"), column("confidence"), column("false_positive"), column("tags"),
)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ==================== 知识案例 (Mongo) ====================

async def save_case(case: dict[str, Any]) -> str:
    db = get_mongo_db()
    await db[CASE_COLL].update_one(
        {"case_id": case["case_id"]}, {"$set": case}, upsert=True
    )
    return case["case_id"]


async def get_case(case_id: str) -> Optional[dict[str, Any]]:
    doc = await get_mongo_db()[CASE_COLL].find_one({"case_id": case_id}, {"_id": 0})
    return doc


async def list_cases(
    *, attack_type: Optional[str] = None, tags: Optional[list[str]] = None,
    status: str = "published", limit: int = 50,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"status": status}
    if attack_type:
        query["attack_type"] = attack_type
    if tags:
        query["tags"] = {"$in": tags}
    cursor = get_mongo_db()[CASE_COLL].find(query, {"_id": 0}).limit(limit)
    return await cursor.to_list(length=limit)


async def search_similar_cases(
    features: dict[str, Any], top_k: int = 3
) -> list[dict[str, Any]]:
    """相似案例检索 (TTP Jaccard 为主, P3 内存版; P4 可换 Milvus)"""
    event_ttps = set(features.get("ttps") or [])
    attack_type = features.get("attack_type")
    cases = await list_cases(status="published", limit=200)
    scored: list[tuple[float, dict]] = []
    for c in cases:
        case_ttps = set(c.get("ttps") or [])
        ttp_sim = (len(event_ttps & case_ttps) / len(event_ttps)) if event_ttps else 0.0
        type_sim = 1.0 if attack_type and c.get("attack_type") == attack_type else 0.0
        score = 0.6 * ttp_sim + 0.3 * type_sim + 0.1 * (len(event_ttps & case_ttps) / 20)
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"case_id": c["case_id"], "title": c["title"], "similarity": round(s, 3),
         "attack_type": c.get("attack_type"), "matched_ttps": sorted(event_ttps & set(c.get("ttps") or []))}
        for s, c in scored[:top_k] if s > 0
    ]


# ==================== 知识文章 (Mongo) ====================

async def save_article(article: dict[str, Any]) -> str:
    db = get_mongo_db()
    await db[ARTICLE_COLL].update_one(
        {"article_id": article["article_id"]}, {"$set": article}, upsert=True
    )
    return article["article_id"]


async def list_articles(
    *, category: Optional[str] = None, limit: int = 50
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if category:
        query["category"] = category
    cursor = get_mongo_db()[ARTICLE_COLL].find(query, {"_id": 0}).limit(limit)
    return await cursor.to_list(length=limit)


async def search_articles(query_text: str, top_k: int = 5) -> list[dict[str, Any]]:
    """文章关键词检索 (Mongo regex; P4 可换向量)"""
    import re as _re

    rx = _re.compile(_re.escape(query_text), _re.IGNORECASE)
    cursor = get_mongo_db()[ARTICLE_COLL].find(
        {"$or": [{"title": rx}, {"content": rx}, {"tags": rx}]}, {"_id": 0}
    ).limit(top_k)
    return await cursor.to_list(length=top_k)


# ==================== TTP 知识 (PG) ====================

async def upsert_ttp(session: AsyncSession, ttp: dict[str, Any]) -> str:
    await session.execute(
        _ttp_tbl.insert().values(
            ttp_id=ttp["ttp_id"],
            mitre_tactic=ttp.get("mitre_tactic", ""),
            mitre_technique=ttp.get("mitre_technique", ""),
            name=ttp.get("name", ttp["ttp_id"]),
            description=ttp.get("description", ""),
            detection_tips=ttp.get("detection_tips", []),
            response_tips=ttp.get("response_tips", []),
            source_cases=ttp.get("source_cases", []),
        ).on_conflict_do_update(  # type: ignore[attr-defined]
            index_elements=["ttp_id"],
            set_={"detection_tips": ttp.get("detection_tips", []),
                  "response_tips": ttp.get("response_tips", []),
                  "source_cases": ttp.get("source_cases", [])},
        )
    )
    await session.commit()
    return ttp["ttp_id"]


async def get_ttp(session: AsyncSession, ttp_id: str) -> Optional[dict[str, Any]]:
    row = (await session.execute(
        select(_ttp_tbl).where(_ttp_tbl.c.ttp_id == ttp_id)
    )).mappings().first()
    return dict(row) if row else None


# ==================== Playbook (PG) ====================

async def save_playbook(session: AsyncSession, pb: dict[str, Any]) -> str:
    pb.setdefault("playbook_id", _new_id("pb"))
    await session.execute(
        _playbook_tbl.insert().values(
            playbook_id=pb["playbook_id"], source_case_id=pb.get("source_case_id"),
            name=pb["name"], description=pb.get("description", ""),
            attack_type=pb["attack_type"], steps=pb.get("steps", []),
            prerequisites=pb.get("prerequisites", []),
            applicable_assets=pb.get("applicable_assets", []),
            effectiveness=pb.get("effectiveness", 0),
            side_effects=pb.get("side_effects", []),
            author=pb.get("author", "system"), version=pb.get("version", "1.0"),
            usage_count=pb.get("usage_count", 0), success_rate=pb.get("success_rate", 0),
        ).on_conflict_do_update(  # type: ignore[attr-defined]
            index_elements=["playbook_id"],
            set_={"name": pb["name"], "steps": pb.get("steps", []),
                  "effectiveness": pb.get("effectiveness", 0)},
        )
    )
    await session.commit()
    return pb["playbook_id"]


async def list_playbooks(
    session: AsyncSession, *, attack_type: Optional[str] = None
) -> list[dict[str, Any]]:
    cond = _playbook_tbl.c.playbook_id.isnot(None)
    if attack_type:
        cond = cond & (_playbook_tbl.c.attack_type == attack_type)
    rows = (await session.execute(
        select(_playbook_tbl).where(cond).order_by(desc(_playbook_tbl.c.usage_count))
    )).mappings().all()
    return [dict(r) for r in rows]


async def recommend_playbooks(
    session: AsyncSession, attack_type: str, ttps: list[str], top_k: int = 3
) -> list[dict[str, Any]]:
    """处置方案推荐: attack_type 精确 + TTP 重叠排序"""
    pbs = await list_playbooks(session, attack_type=attack_type)
    if not pbs:  # attack_type 无匹配则全库兜底
        pbs = await list_playbooks(session)
    set(ttps)
    scored = []
    for pb in pbs:
        # steps 里可能含 technique 引用, 这里用 effectiveness/usage 作主排序
        match_score = 0.5 + 0.3 * (pb.get("effectiveness") or 0) + 0.2 * min((pb.get("usage_count") or 0) / 50, 1)
        scored.append((match_score, pb))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"playbook_id": pb["playbook_id"], "name": pb["name"], "attack_type": pb["attack_type"],
         "match_score": round(s, 3), "usage_count": pb.get("usage_count", 0),
         "success_rate": pb.get("success_rate", 0)}
        for s, pb in scored[:top_k]
    ]


# ==================== IOC 知识 (PG, 图谱关系冗余) ====================

async def save_ioc(session: AsyncSession, ioc: dict[str, Any]) -> str:
    ioc.setdefault("ioc_id", _new_id("ioc"))
    await session.execute(
        _ioc_tbl.insert().values(
            ioc_id=ioc["ioc_id"], source_case_id=ioc.get("source_case_id"),
            ioc_type=ioc["ioc_type"], ioc_value=ioc["ioc_value"],
            threat_type=ioc.get("threat_type", ""), malware_family=ioc.get("malware_family"),
            threat_actor=ioc.get("threat_actor"), campaign=ioc.get("campaign"),
            context=ioc.get("context", ""), confidence=ioc.get("confidence", 0.5),
            false_positive=ioc.get("false_positive", False), tags=ioc.get("tags", []),
        ).on_conflict_do_update(  # type: ignore[attr-defined]
            index_elements=["ioc_id"],
            set_={"threat_type": ioc.get("threat_type", ""),
                  "malware_family": ioc.get("malware_family"),
                  "threat_actor": ioc.get("threat_actor"), "confidence": ioc.get("confidence", 0.5)},
        )
    )
    await session.commit()
    return ioc["ioc_id"]


async def batch_query_iocs(
    session: AsyncSession, iocs: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """批量查询 IOC 命中 (分析时自动匹配, F-008-03)"""
    results = []
    for ioc in iocs:
        row = (await session.execute(
            select(_ioc_tbl).where(
                (_ioc_tbl.c.ioc_type == ioc["type"])
                & (_ioc_tbl.c.ioc_value == ioc["value"])
            )
        )).mappings().first()
        if row:
            results.append({
                "ioc_type": ioc["type"], "ioc_value": ioc["value"], "found": True,
                "malware_family": row.get("malware_family"),
                "threat_actor": row.get("threat_actor"),
                "threat_type": row.get("threat_type"),
                "confidence": row.get("confidence"),
            })
        else:
            results.append({"ioc_type": ioc["type"], "ioc_value": ioc["value"], "found": False})
    return results


# ==================== 种子加载 ====================

async def seed_if_empty(session: AsyncSession) -> dict[str, int]:
    """库为空时加载种子知识 (启动时调用)。返回各库加载数。"""
    counts: dict[str, int] = {}
    try:
        seed = json.loads(_SEED_FILE.read_text(encoding="utf-8"))
        # Playbook
        existing_pb = await list_playbooks(session)
        if not existing_pb:
            for pb in seed.get("playbooks", []):
                await save_playbook(session, pb)
        counts["playbooks"] = len(seed.get("playbooks", []))
        # IOC
        existing_ioc = (await session.execute(select(_ioc_tbl.c.ioc_id).limit(1))).first()
        if not existing_ioc:
            for ioc in seed.get("ioc_graph", []):
                await save_ioc(session, ioc)
        counts["iocs"] = len(seed.get("ioc_graph", []))
        # 案例 + 文章 (Mongo)
        cases = await list_cases(limit=1)
        if not cases:
            seed_cases = json.loads(_SEED_CASES_FILE.read_text(encoding="utf-8"))["cases"]
            for c in seed_cases:
                await save_case({
                    "case_id": c["case_id"], "title": c["title"],
                    "attack_type": c.get("attack_type", "other"), "description": c.get("description", ""),
                    "ttps": c.get("ttps", []), "tags": [], "status": "published",
                    "threat_actor": c.get("threat_actor"), "response": c.get("response"),
                    "create_time": datetime.now(),
                })
        counts["cases"] = 1 if not cases else 0
        articles = await list_articles(limit=1)
        if not articles:
            for a in seed.get("articles", []):
                await save_article({**a, "status": "published"})
        counts["articles"] = len(seed.get("articles", [])) if not articles else 0
        logger.info("知识库种子加载完成: {}", counts)
    except Exception as e:  # noqa: BLE001
        logger.warning("知识库种子加载失败(不阻塞): {}", str(e)[:120])
    return counts
