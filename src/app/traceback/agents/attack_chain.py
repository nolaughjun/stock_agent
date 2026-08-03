"""攻击链分析智能体

对齐 01_phase1_design.md §4.3
流程: 加载时间线 → 入口点识别(反向溯源至 initial_access) → 横向移动路径
      → TTP 标注(MITRE 白名单校验) → 受影响范围 + 数据外传 → 攻击路径图
TTP 准确率保障: technique_id 须命中 ATT&CK 白名单, 未命中标 unknown 降置信 (§4.3)
"""
from __future__ import annotations

from typing import Any

from src.app.traceback.agents.base import AgentBase
from src.app.traceback.core.config import settings
from src.app.traceback.core.db import session_scope
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.state import TracebackState
from src.app.traceback.repositories import attack_chain_repo, evidence_repo
from src.app.traceback.services.mitre import validate_ttp


class AttackChainAgent(AgentBase):
    name = "attack_chain"

    async def run(self, state: TracebackState) -> dict[str, Any]:
        task_id = state["task_id"]
        events: list[dict[str, Any]] = state.get("timeline_events") or []
        logger.info("[{}] 攻击链分析开始, 事件 {} 条", task_id, len(events))

        chain = self._build_chain(task_id, events)
        # 持久化
        async with session_scope() as session:
            await attack_chain_repo.save_chain(session, task_id, chain)
        logger.info(
            "[{}] 攻击链完成: 入口={} 横向={} 受影响资产={} TTP={}",
            task_id,
            chain["entry_point"]["asset"] if chain["entry_point"] else None,
            len(chain["lateral_movements"]),
            len(chain["affected_assets"]),
            len(chain["ttp_summary"]),
        )
        return {"stage": "chain_analyzed", "attack_chain": chain}

    def _build_chain(
        self, task_id: str, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        # 1. 入口点: 最早的 initial_access 事件
        entry = self._identify_entry(events)
        # 2. 横向移动
        laterals = self._identify_lateral(events)
        # 3. TTP 标注 (白名单校验)
        ttp_summary = self._annotate_ttp(events)
        # 4. 受影响范围 + 数据外传
        affected = self._affected_assets(events, laterals)
        exfil = any(e.get("event_category") == "exfiltration" for e in events)
        # 5. 攻击者基础设施 (从 C2/外部资产提取)
        attacker_ips, attacker_domains = self._extract_attacker_infra(events)
        attacker_tools = self._extract_tools(events)
        # 6. 攻击路径图
        path_graph = self._build_path_graph(events, entry, laterals)
        # 阶段聚合
        phases = self._build_phases(events)

        return {
            "chain_id": f"chain-{task_id}",
            "task_id": task_id,
            "phases": phases,
            "entry_point": entry,
            "lateral_movements": laterals,
            "affected_assets": affected,
            "data_exfiltration": exfil,
            "attacker_ips": attacker_ips,
            "attacker_domains": attacker_domains,
            "attacker_tools": attacker_tools,
            "ttp_summary": ttp_summary,
            "path_graph": path_graph,
        }

    def _identify_entry(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        """反向溯源: 从最高 severity 回溯到最早的 initial_access"""
        ia_events = [e for e in events if e.get("event_category") == "initial_access"]
        if not ia_events:
            # 无明确入口 → 取最早事件作弱入口
            if not events:
                return None
            earliest = min(events, key=lambda e: e.get("event_time"))
            return self._entry_from_event(earliest, confidence=0.4)
        earliest = min(ia_events, key=lambda e: e.get("event_time"))
        return self._entry_from_event(earliest, confidence=0.8)

    def _entry_from_event(self, ev: dict[str, Any], confidence: float) -> dict[str, Any]:
        assets = ev.get("related_assets") or []
        technique = (ev.get("attack_techniques") or [None])[0]
        tid, _, _ = validate_ttp(technique, confidence)
        # 入口资产取受害侧(内网主机), 而非攻击者来源
        victim = _pick_victim_asset(assets)
        return {
            "asset": victim or "unknown",
            "vector": ev.get("event_type", ""),
            "technique_id": tid,
            "evidence_ids": ev.get("source_evidence") or [],
            "confidence": confidence,
        }

    def _identify_lateral(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """从横向移动事件推断资产跳转链"""
        out: list[dict[str, Any]] = []
        for ev in events:
            if ev.get("event_category") != "lateral_movement":
                continue
            assets = ev.get("related_assets") or []
            if len(assets) < 2:
                continue
            technique = (ev.get("attack_techniques") or [None])[0]
            tid, _, _ = validate_ttp(technique, 0.7)
            out.append({
                "from_asset": assets[0],
                "to_asset": assets[1],
                "technique_id": tid,
                "technique_label": ev.get("title", ""),
                "evidence_ids": ev.get("source_evidence") or [],
            })
        return out

    def _annotate_ttp(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """汇总并校验所有 TTP (白名单约束, §4.3 准确率保障)"""
        seen: dict[str, dict[str, Any]] = {}
        for ev in events:
            for t in ev.get("attack_techniques") or []:
                if not t:
                    continue
                tid, tactic, conf = validate_ttp(t, ev.get("confidence", 0.7))
                key = tid or t
                if key not in seen:
                    seen[key] = {
                        "technique_id": tid,  # None 表示未命中白名单
                        "raw_id": t,
                        "tactic": tactic,
                        "confidence": conf,
                        "event_ids": [],
                    }
                seen[key]["event_ids"].append(ev.get("event_id"))
        return list(seen.values())

    def _affected_assets(
        self, events: list[dict[str, Any]], laterals: list[dict[str, Any]]
    ) -> list[str]:
        """受影响资产 = 受害侧(排除攻击者 IP/外部标记)"""
        assets: set[str] = set()
        for ev in events:
            for a in ev.get("related_assets") or []:
                if a and a != "unknown" and a != "external" and not _is_external_ip(a):
                    assets.add(a)
        for lm in laterals:
            assets.add(lm["from_asset"])
            assets.add(lm["to_asset"])
        return sorted(a for a in assets if a and a != "unknown")

    def _extract_attacker_infra(
        self, events: list[dict[str, Any]]
    ) -> tuple[list[str], list[str]]:
        """从 C2 事件/外部资产提取攻击者 IP/域名"""
        ips: set[str] = set()
        domains: set[str] = set()
        for ev in events:
            if ev.get("event_category") != "command_and_control":
                continue
            for a in ev.get("related_assets") or []:
                if _is_external_ip(a):
                    ips.add(a)
                elif "." in a and not a[0].isdigit():
                    domains.add(a)
        return sorted(ips), sorted(domains)

    def _extract_tools(self, events: list[dict[str, Any]]) -> list[str]:
        tools: set[str] = set()
        for ev in events:
            t = ev.get("event_type", "")
            if "powershell" in t:
                tools.add("PowerShell")
            if "rdp" in t:
                tools.add("RDP")
        return sorted(tools)

    def _build_path_graph(
        self, events: list[dict[str, Any]],
        entry: dict[str, Any] | None,
        laterals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        nodes: dict[str, dict[str, str]] = {}
        edges: list[dict[str, Any]] = []

        # 入口边: attacker → entry asset
        if entry and entry["asset"] != "unknown":
            nodes.setdefault(entry["asset"], {"id": entry["asset"], "type": "victim"})
            for ip in self._extract_attacker_infra(events)[0]:
                nodes.setdefault(ip, {"id": ip, "type": "attacker"})
                edges.append({"from": ip, "to": entry["asset"],
                              "technique": entry.get("technique_id"),
                              "label": entry.get("vector", "initial_access")})
        # 横向移动边
        for lm in laterals:
            nodes.setdefault(lm["from_asset"], {"id": lm["from_asset"], "type": "victim"})
            nodes.setdefault(lm["to_asset"], {"id": lm["to_asset"], "type": "victim"})
            edges.append({"from": lm["from_asset"], "to": lm["to_asset"],
                          "technique": lm.get("technique_id"), "label": lm.get("technique_label", "")})
        return {"nodes": list(nodes.values()), "edges": edges}

    def _build_phases(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按战术聚合攻击阶段 (对齐 kill chain)"""
        by_tactic: dict[str, list[str]] = {}
        for ev in events:
            cat = ev.get("event_category", "unknown")
            if cat == "unknown":
                continue
            by_tactic.setdefault(cat, []).append(ev.get("event_id"))
        return [{"tactic": t, "event_ids": eids} for t, eids in by_tactic.items()]


def _is_external_ip(ip: str) -> bool:
    """粗略判断是否外部 IP (非私有网段)"""
    if not ip or not ip[0].isdigit():
        return False
    if ip.startswith(("10.", "172.16.", "172.17.", "172.18.", "172.19.",
                      "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                      "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                      "172.30.", "172.31.", "192.168.", "127.")):
        return False
    return True


def _pick_victim_asset(assets: list[str]) -> str | None:
    """从资产列表选受害侧(内网主机): 优先私有 IP, 其次非外部标记"""
    for a in assets:
        if a and a != "external" and not _is_external_ip(a):
            return a
    for a in assets:
        if a and a != "external":
            return a
    return None
