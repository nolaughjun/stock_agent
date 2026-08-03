"""证据收集智能体

对齐 01_phase1_design.md §4.1
流程: 构建 CollectContext → (LLM 查询提示) → 并行适配器收集 → 去重 → (LLM 评估) → 三段式存储
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from pydantic import BaseModel, Field

from src.app.traceback.adapters.manager import ComponentManager, get_manager
from src.app.traceback.agents.base import AgentBase
from src.app.traceback.core.config import settings
from src.app.traceback.core.db import session_scope
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.state import TracebackState
from src.app.traceback.repositories import evidence_repo, task_repo
from src.app.traceback.schemas.evidence import (
    CollectContext,
    EvidenceScore,
    RawEvidence,
)

# 参与证据收集的适配器 (timesketch 是时间线 sink, 不参与收集)
_COLLECT_SOURCES = ["siem", "edr", "dns", "misp"]


class CollectHints(BaseModel):
    """LLM 从任务描述提取的查询提示"""
    domains: list[str] = Field(default_factory=list, description="可疑域名")
    hashes: list[str] = Field(default_factory=list, description="可疑文件 hash")
    focus: str = Field("", description="收集重点")


class EvidenceCollector(AgentBase):
    """证据收集智能体"""

    name = "evidence_collector"

    def __init__(self, manager: ComponentManager | None = None, llm: Any = None) -> None:
        super().__init__(llm=llm)
        self.manager = manager or get_manager()

    async def run(self, state: TracebackState) -> dict[str, Any]:
        task_id = state["task_id"]
        logger.info("[{}] 证据收集开始", task_id)

        # 1. 取任务 + 构建 context
        async with session_scope() as session:
            task = await task_repo.get_task(session, task_id)
        if not task:
            return {"stage": "collecting", "errors": [f"任务不存在: {task_id}"], "evidence_ids": []}

        ctx = CollectContext(
            task_id=task_id,
            asset_ips=task.get("asset_ips") or [],
            asset_hosts=task.get("asset_hosts") or [],
            asset_users=task.get("asset_users") or [],
            event_start_time=task.get("event_start_time"),
            event_end_time=task.get("event_end_time"),
        )
        # 2. LLM 查询提示 (失败降级为空, 不阻塞)
        hints = await self._gen_hints(task.get("description") or task.get("task_name") or "")
        ctx.query_hint = {"domains": hints.domains, "hashes": hints.hashes, "focus": hints.focus}

        # 3. 并行适配器收集 (单源超时降级, §4.1)
        raw_items = await self._collect_parallel(ctx)

        # 4. 去重 (按 source + raw hash)
        raw_items = self._dedup(raw_items)
        logger.info("[{}] 收集到原始证据 {} 条(去重后)", task_id, len(raw_items))

        # 5. LLM 评估打分 (失败降级用适配器默认 importance)
        scored = await self._evaluate(raw_items)

        # 6. 三段式存储
        evidence_ids: list[str] = []
        async with session_scope() as session:
            for item, score in zip(raw_items, scored):
                eid = await evidence_repo.save_evidence(
                    session,
                    task_id=task_id,
                    evidence_type=item.evidence_type,
                    source=item.source,
                    raw_data=item.raw,
                    parsed_data={**item.parsed, **(score.model_dump(exclude_none=True) if score else {})},
                    importance=score.importance if score else item.importance,
                    tags=score.tags if score else [],
                    related_assets=score.related_assets if score else item.related_assets,
                )
                evidence_ids.append(eid)

        logger.info("[{}] 证据收集完成, 存证 {} 条", task_id, len(evidence_ids))
        return {"stage": "collecting", "evidence_ids": evidence_ids}

    async def _gen_hints(self, description: str) -> CollectHints:
        """LLM 从描述提取可疑域名/hash; 无 LLM 或失败返回空 (降级)"""
        if not settings.deepseek_api_key or not description:
            return CollectHints()
        try:
            result = await self.llm.complete_json(
                system="你是安全分析师。从事件描述中提取可疑域名和文件hash, 供威胁情报查询。",
                user=f"事件描述:\n{description}",
                schema_model=CollectHints,
            )
            return result  # type: ignore[return-value]
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 查询提示生成失败, 降级: {}", str(e)[:120])
            return CollectHints()

    async def _collect_parallel(self, ctx: CollectContext) -> list[RawEvidence]:
        """并行调用所有可用适配器, 单源超时/失败降级不阻塞整体"""
        async def _one(name: str) -> list[RawEvidence]:
            adapter = self.manager.get(name)
            if adapter is None:
                logger.debug("适配器 {} 不可用, 跳过", name)
                return []
            try:
                return await asyncio.wait_for(
                    adapter.collect(ctx), timeout=settings.evidence_collect_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("适配器 {} 超时, 降级", name)
                return []
            except Exception as e:  # noqa: BLE001
                logger.warning("适配器 {} 异常, 降级: {}", name, str(e)[:120])
                return []

        results = await asyncio.gather(*(_one(n) for n in _COLLECT_SOURCES))
        return [item for sub in results for item in sub]

    def _dedup(self, items: list[RawEvidence]) -> list[RawEvidence]:
        seen: set[str] = set()
        out: list[RawEvidence] = []
        for it in items:
            key = f"{it.source}:{_raw_hash(it.raw)}"
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    async def _evaluate(self, items: list[RawEvidence]) -> list[EvidenceScore | None]:
        """LLM 评估证据重要性/标签; 无 LLM 或失败返回 None (用适配器默认)"""
        if not settings.deepseek_api_key or not items:
            return [None] * len(items)
        out: list[EvidenceScore | None] = []
        batch = settings.llm_batch_size
        for i in range(0, len(items), batch):
            chunk = items[i : i + batch]
            try:
                scores = await self._eval_batch(chunk)
                out.extend(scores)
            except Exception as e:  # noqa: BLE001
                logger.warning("LLM 证据评估失败, 降级默认: {}", str(e)[:120])
                out.extend([None] * len(chunk))
        return out

    async def _eval_batch(self, chunk: list[RawEvidence]) -> list[EvidenceScore]:
        import json

        # 给每条临时 id 供 LLM 引用
        indexed = [{"idx": i, "type": c.evidence_type, "source": c.source,
                    "raw": _truncate(c.raw)} for i, c in enumerate(chunk)]
        system = (
            "你是安全分析师。对每条证据评估重要程度(high/medium/low)、打标签、关联资产。"
            "只输出 JSON 数组。"
        )
        user = json.dumps(indexed, ensure_ascii=False, default=str)
        raw = await self.llm.complete(system=system, user=user)
        data = _extract_json_array(raw)
        score_map: dict[int, EvidenceScore] = {}
        for d in data:
            idx = d.get("idx")
            if idx is None:
                continue
            try:
                imp = d.get("importance", "medium")
                if imp not in ("high", "medium", "low"):
                    imp = "medium"
                score_map[int(idx)] = EvidenceScore(
                    evidence_id=str(idx), importance=imp,  # type: ignore[arg-type]
                    tags=d.get("tags", []), related_assets=d.get("related_assets", []),
                    summary=d.get("summary", ""),
                )
            except Exception:  # noqa: BLE001
                continue
        return [score_map.get(i) for i in range(len(chunk))]


def _raw_hash(raw: Any) -> str:
    return hashlib.md5(str(raw).encode("utf-8")).hexdigest()


def _truncate(raw: Any, limit: int = 800) -> Any:
    s = str(raw)
    return s[:limit] + "..." if len(s) > limit else raw


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    import json as _json

    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = _json.loads(text)
        return data if isinstance(data, list) else []
    except _json.JSONDecodeError:
        return []
