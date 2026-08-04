"""知识沉淀智能体 (P3)

对齐 03_phase3_design.md §6
任务完成后从分析结果自动提取: 案例草稿 / IOC 条目 / TTP 反向引用 / 处置方案草稿。
所有条目初始 draft, 经人工审核④发布后入索引参与推荐 (F-007-01)。
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.agents.base import AgentBase
from src.app.traceback.core.db import session_scope
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.state import TracebackState
from src.app.traceback.repositories import knowledge_repo


class KnowledgeSink(AgentBase):
    name = "knowledge_sink"

    async def run(self, state: TracebackState) -> dict[str, Any]:
        task_id = state["task_id"]
        attack_chain = state.get("attack_chain") or {}
        attribution = state.get("attribution") or {}
        task = state.get("task") or {}
        logger.info("[{}] 知识沉淀开始", task_id)

        ioc_ids: list[str] = []
        ttp_refs: list[str] = []
        case_id: str | None = None
        playbook_id: str | None = None

        try:
            async with session_scope() as session:
                # 1. 案例草稿 (Mongo, status=draft)
                case_id = await self._extract_case(task_id, task, attack_chain, attribution)
                # 2. IOC 条目 (PG)
                ioc_ids = await self._extract_iocs(session, task_id, attack_chain, case_id)
                # 3. TTP 反向引用 (PG)
                ttp_refs = await self._update_ttp_refs(session, attack_chain, case_id)
                # 4. 处置方案草稿 (PG)
                playbook_id = await self._extract_playbook(session, task_id, attack_chain, case_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[{}] 知识沉淀失败(不阻塞流程): {}", task_id, str(e)[:150])

        result = {
            "case_id": case_id, "ioc_ids": ioc_ids, "ttp_refs": ttp_refs,
            "playbook_id": playbook_id, "note": "草稿已生成, 待人工审核④发布",
        }
        logger.info("[{}] 知识沉淀完成: 案例/IOC{}/TTP{}/Playbook",
                     task_id, len(ioc_ids), len(ttp_refs))
        return {"stage": "knowledge_sunk", "pending_knowledge": result}

    async def _extract_case(
        self, task_id: str, task: dict, attack_chain: dict, attribution: dict
    ) -> str:
        ttps = [t.get("technique_id") for t in (attack_chain.get("ttp_summary") or [])
                if t.get("technique_id")]
        iocs = [{"type": "ip", "value": ip} for ip in attack_chain.get("attacker_ips") or []]
        iocs += [{"type": "domain", "value": d} for d in attack_chain.get("attacker_domains") or []]
        attack_type = self._infer_attack_type(ttps, attribution)
        case = {
            "case_id": f"case-{task_id}",
            "source_task_id": task_id,
            "title": task.get("task_name") or f"溯源案例 {task_id}",
            "description": task.get("description") or "",
            "attack_type": attack_type,
            "severity": "high",
            "tags": [attack_type],
            "ttps": ttps,
            "iocs": iocs,
            "response_actions": [],
            "threat_actor": attribution.get("threat_actor"),
            "status": "draft",  # 待审核④发布
        }
        await knowledge_repo.save_case(case)
        return case["case_id"]

    async def _extract_iocs(
        self, session, task_id: str, attack_chain: dict, case_id: str
    ) -> list[str]:
        ids: list[str] = []
        for ip in attack_chain.get("attacker_ips") or []:
            ids.append(await knowledge_repo.save_ioc(session, {
                "ioc_id": f"ioc-{task_id}-ip-{ip.replace('.', '-')}",
                "source_case_id": case_id, "ioc_type": "ip", "ioc_value": ip,
                "threat_type": "C2", "context": f"来自任务 {task_id} 攻击链",
            }))
        for domain in attack_chain.get("attacker_domains") or []:
            ids.append(await knowledge_repo.save_ioc(session, {
                "ioc_id": f"ioc-{task_id}-dom-{domain.replace('.', '-')[:20]}",
                "source_case_id": case_id, "ioc_type": "domain", "ioc_value": domain,
                "threat_type": "C2", "context": f"来自任务 {task_id}",
            }))
        return ids

    async def _update_ttp_refs(
        self, session, attack_chain: dict, case_id: str
    ) -> list[str]:
        from src.app.traceback.services.mitre import tactic_of

        refs: list[str] = []
        for t in attack_chain.get("ttp_summary") or []:
            tid = t.get("technique_id")
            if not tid:
                continue
            existing = await knowledge_repo.get_ttp(session, tid)
            if existing:
                src_cases = existing.get("source_cases") or []
                if case_id not in src_cases:
                    src_cases.append(case_id)
                await knowledge_repo.upsert_ttp(session, {**existing, "source_cases": src_cases})
            else:
                tactic = tactic_of(tid) or "unknown"
                await knowledge_repo.upsert_ttp(session, {
                    "ttp_id": tid, "mitre_tactic": tactic, "mitre_technique": tid.split(".")[0],
                    "name": tid, "source_cases": [case_id],
                })
            refs.append(tid)
        return refs

    async def _extract_playbook(
        self, session, task_id: str, attack_chain: dict, case_id: str
    ) -> str | None:
        ttps = [t.get("technique_id") for t in (attack_chain.get("ttp_summary") or [])
                if t.get("technique_id")]
        attack_type = self._infer_attack_type(ttps, {})
        pb = {
            "playbook_id": f"pb-{task_id}",
            "source_case_id": case_id,
            "name": f"处置方案-{task_id}",
            "attack_type": attack_type,
            "steps": [
                {"step_id": "s1", "order": 1, "action_type": "isolate",
                 "description": f"隔离受影响主机: {', '.join(attack_chain.get('affected_assets') or [])[:100]}"},
                {"step_id": "s2", "order": 2, "action_type": "eradicate", "description": "清除恶意样本与持久化"},
                {"step_id": "s3", "order": 3, "action_type": "harden",
                 "description": "封禁C2与加固入口"},
            ],
            "effectiveness": 0.0, "usage_count": 0, "success_rate": 0.0,
        }
        return await knowledge_repo.save_playbook(session, pb)

    def _infer_attack_type(self, ttps: list[str], attribution: dict) -> str:
        tset = set(ttps)
        if "T1486" in tset:
            return "ransomware"
        if "T1041" in tset or attribution.get("threat_actor", "").lower().startswith("apt"):
            return "apt"
        if "T1496" in tset:
            return "cryptomining"
        if "T1566" in tset or "T1566.001" in tset or "T1566.002" in tset:
            return "phishing"
        return "other"
