"""威胁归因智能体 (P2, 替换 P1 桩)

对齐 02_phase2_design.md §3
流程: 特征提取(TTPs/工具/基础设施/动机) → OpenCTI 情报匹配 + 相似案例
      → 三因子置信度评估(TTP 0.5 / IOC 0.3 / 工具 0.2) → LLM 攻击者画像(规则兜底)
LLM 仅做画像叙述, 归因结论由多因子算法决定 (TR-001 对策, 防幻觉)。
对外接口与 P1 桩一致: run(state) -> {attribution, similar_cases}。
"""
from __future__ import annotations

import uuid
from typing import Any

from src.app.traceback.adapters.manager import ComponentManager, get_manager
from src.app.traceback.agents.base import AgentBase
from src.app.traceback.core.config import settings
from src.app.traceback.core.db import session_scope
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.state import TracebackState
from src.app.traceback.repositories import attack_chain_repo, attribution_repo
from src.app.traceback.schemas.attribution import (
    ActorFeatures,
    ActorProfile,
    AttributionResult,
    CandidateScore,
)
from src.app.traceback.services.similar_cases import SimilarCaseMatcher

# 三因子权重 (§3.3)
W_TTP = 0.5
W_IOC = 0.3
W_TOOL = 0.2
# 归因阈值: 低于此置信度不归因
ATTRIBUTION_THRESHOLD = 40.0


class AttributionAgent(AgentBase):
    name = "attribution"

    def __init__(
        self, manager: ComponentManager | None = None,
        matcher: SimilarCaseMatcher | None = None, llm: Any = None,
    ) -> None:
        super().__init__(llm=llm)
        self.manager = manager or get_manager()
        self.matcher = matcher or SimilarCaseMatcher()

    async def run(self, state: TracebackState) -> dict[str, Any]:
        task_id = state["task_id"]
        logger.info("[{}] 归因分析开始", task_id)

        attack_chain = state.get("attack_chain")
        if not attack_chain:
            async with session_scope() as session:
                attack_chain = await attack_chain_repo.get_chain(session, task_id)

        # 1. 特征提取
        features = self._extract_features(attack_chain, state.get("timeline_events") or [])

        # 2. OpenCTI 候选威胁组织
        actors = await self._get_actors()

        # 3. 三因子匹配
        candidates = self._score_candidates(features, actors)
        logger.info("[{}] 归因候选: {}", task_id,
                    [(c.threat_actor, round(c.confidence, 1)) for c in candidates[:3]])

        # 4. 相似案例
        attack_type = self._infer_attack_type(features, attack_chain)
        similar = await self.matcher.search(features, attack_type, top_k=3)

        # 5. 归因结论 (Top1)
        top = candidates[0] if candidates else None
        attributed = bool(top and top.confidence >= ATTRIBUTION_THRESHOLD)

        # 6. 攻击者画像 (LLM 主, 规则兜底)
        actor_profile = await self._gen_profile(top, actors, attack_type) if attributed else None

        result = AttributionResult(
            attribution_id=f"attr-{uuid.uuid4().hex[:10]}",
            task_id=task_id,
            attributed=attributed,
            threat_actor=top.threat_actor if attributed else None,
            confidence_score=round(top.confidence, 2) if top else 0.0,
            matched_iocs=[{"value": v, "actor": top.threat_actor} for v in top.matched_iocs]
            if top else [],
            matched_ttps=[{"id": t, "actor": top.threat_actor} for t in top.matched_ttps]
            if top else [],
            matched_campaigns=self._campaigns_of(top, actors),
            similar_cases=similar,
            actor_profile=actor_profile,
            candidates=candidates[:3],
        )
        logger.info("[{}] 归因结论: actor={} conf={} attributed={}",
                    task_id, result.threat_actor, result.confidence_score, attributed)

        # 持久化到 PG (供 /attribution API 查询)
        try:
            async with session_scope() as session:
                await attribution_repo.save_attribution(session, task_id, result.model_dump())
        except Exception as e:  # noqa: BLE001
            logger.warning("[{}] 归因结果持久化失败(不影响流程): {}", task_id, str(e)[:120])

        return {
            "stage": "attributed",
            "attribution": result.model_dump(),
            "similar_cases": similar,
        }

    # ---- 特征提取 ----

    def _extract_features(
        self, attack_chain: dict | None, timeline: list[dict]
    ) -> ActorFeatures:
        ttps: set[str] = set()
        if attack_chain:
            for t in attack_chain.get("ttp_summary") or []:
                # 仅用白名单校验过的 technique_id (None=未命中, 跳过, 防止脏 TTP 污染归因)
                tid = t.get("technique_id")
                if tid:
                    ttps.add(tid)
        # 兜底: 从时间线事件聚合
        if not ttps:
            for ev in timeline:
                for t in ev.get("attack_techniques") or []:
                    if t:
                        ttps.add(t)

        infra: set[str] = set()
        tools: set[str] = set()
        if attack_chain:
            infra.update(attack_chain.get("attacker_ips") or [])
            infra.update(attack_chain.get("attacker_domains") or [])
            tools.update(t.lower() for t in (attack_chain.get("attacker_tools") or []))

        motive = self._infer_motive(attack_chain, ttps)
        return ActorFeatures(
            ttps=sorted(ttps), tools=sorted(tools),
            infrastructure=sorted(infra), motive=motive,
        )

    def _infer_motive(self, attack_chain: dict | None, ttps: set[str]) -> str:
        if "T1486" in ttps or (attack_chain and attack_chain.get("data_exfiltration")):
            return "financial"
        if "T1041" in ttps or "T1566.002" in ttps:
            return "espionage"
        return "unknown"

    def _infer_attack_type(self, features: ActorFeatures, attack_chain: dict | None) -> str:
        ttps = set(features.ttps)
        if "T1486" in ttps:
            return "ransomware"
        if "T1041" in ttps or features.motive == "espionage":
            return "apt"
        if "T1496" in ttps:
            return "cryptomining"
        return "unknown"

    # ---- 候选评分 (三因子, §3.3) ----

    def _score_candidates(
        self, features: ActorFeatures, actors: list[dict]
    ) -> list[CandidateScore]:
        event_ttps = set(features.ttps)
        event_iocs = set(features.infrastructure)
        event_tools = set(features.tools)
        scores: list[CandidateScore] = []
        for a in actors:
            actor_ttps = set(a.get("ttps") or [])
            actor_iocs = set(a.get("iocs") or [])
            actor_tools = set(t.lower() for t in (a.get("tools") or []))

            matched_ttp = event_ttps & actor_ttps
            matched_ioc = event_iocs & actor_iocs
            matched_tool = event_tools & actor_tools

            score_ttp = len(matched_ttp) / len(event_ttps) if event_ttps else 0.0
            score_ioc = len(matched_ioc) / len(event_iocs) if event_iocs else 0.0
            score_tool = len(matched_tool) / len(event_tools) if event_tools else 0.0
            confidence = 100 * (W_TTP * score_ttp + W_IOC * score_ioc + W_TOOL * score_tool)

            scores.append(CandidateScore(
                threat_actor=a["name"], confidence=round(confidence, 2),
                score_ttp=round(score_ttp, 3), score_ioc=round(score_ioc, 3),
                score_tool=round(score_tool, 3),
                matched_ttps=sorted(matched_ttp), matched_iocs=sorted(matched_ioc),
                matched_tools=sorted(matched_tool),
            ))
        scores.sort(key=lambda c: c.confidence, reverse=True)
        return scores

    def _campaigns_of(self, top: CandidateScore | None, actors: list[dict]) -> list[str]:
        if not top:
            return []
        for a in actors:
            if a["name"] == top.threat_actor:
                return a.get("campaigns") or []
        return []

    async def _get_actors(self) -> list[dict]:
        adapter = self.manager.get("opencti")
        if adapter is None:
            return []
        return await adapter.get_actors()  # type: ignore[attr-defined]

    # ---- 攻击者画像 ----

    async def _gen_profile(
        self, top: CandidateScore, actors: list[dict], attack_type: str
    ) -> ActorProfile:
        actor = next((a for a in actors if a["name"] == top.threat_actor), {})
        if settings.deepseek_api_key:
            try:
                return await self._gen_profile_llm(top, actor, attack_type)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                logger.warning("LLM 画像生成失败, 降级规则: {}", str(e)[:120])
        return self._rule_profile(top, actor)

    def _rule_profile(self, top: CandidateScore, actor: dict) -> ActorProfile:
        basis = (
            f"TTP 重叠 {len(top.matched_ttps)} 项 ({', '.join(top.matched_ttps[:5]) or '无'}), "
            f"IOC 命中 {len(top.matched_iocs)}, 工具匹配 {len(top.matched_tools)}; "
            f"综合置信度 {top.confidence:.1f}/100。"
        )
        return ActorProfile(
            name=top.threat_actor,
            description=actor.get("description", ""),
            sophistication=actor.get("sophistication", ""),
            primary_motivation=actor.get("primary_motivation", ""),
            goals=actor.get("goals") or [],
            campaigns=actor.get("campaigns") or [],
            typical_ttps=actor.get("ttps") or [],
            match_basis=basis,
        )

    async def _gen_profile_llm(
        self, top: CandidateScore, actor: dict, attack_type: str
    ) -> ActorProfile:
        import json
        system = (
            "你是威胁情报分析师。基于归因匹配结果生成攻击者画像, 客观陈述, 不臆造。"
            "输出符合 ActorProfile 的 JSON: name/description/sophistication/primary_motivation/"
            "goals/campaigns/typical_ttps/match_basis。"
        )
        user = json.dumps({
            "threat_actor": top.threat_actor, "confidence": top.confidence,
            "matched_ttps": top.matched_ttps, "matched_iocs": top.matched_iocs,
            "matched_tools": top.matched_tools, "attack_type": attack_type,
            "known_actor_info": actor,
        }, ensure_ascii=False, default=str)
        result = await self.llm.complete_json(
            system=system, user=user, schema_model=ActorProfile
        )
        return result  # type: ignore[return-value]
