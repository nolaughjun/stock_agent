"""时间线构建智能体

对齐 01_phase1_design.md §4.2
流程: 加载证据 → 提取事件(LLM map-reduce / 规则兜底) → 去重 → 时序排序
      → 关键节点标记 → 写入 Mongo(可选推 Timesketch)
关键事件类型对齐 MITRE 战术: initial_access/lateral_movement/persistence/exfiltration/impact/...
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.app.traceback.adapters.manager import get_manager
from src.app.traceback.agents.base import AgentBase
from src.app.traceback.core.config import settings
from src.app.traceback.core.db import session_scope
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.state import TracebackState
from src.app.traceback.repositories import evidence_repo, timeline_repo
from src.app.traceback.schemas.timeline import EventCategory, SeverityLevel

# mock/已知事件 → (event_category, [technique_id], severity) 映射 (规则兜底)
_EVENT_MAP: dict[str, tuple[EventCategory, list[str], SeverityLevel]] = {
    "phishing_email_received": ("initial_access", ["T1566.001"], "high"),
    "powershell_execution": ("execution", ["T1059.001"], "high"),
    "c2_beacon": ("command_and_control", ["T1071.001"], "high"),
    "rdp_lateral": ("lateral_movement", ["T1021.001"], "high"),
    "file_encryption": ("impact", ["T1486"], "critical"),
    "credential_dump_suspected": ("credential_access", ["T1003.001"], "high"),
    "mass_file_write": ("impact", ["T1486"], "critical"),
}

# 关键节点战术 (§4.2)
_KEY_TACTICS = {
    "initial_access", "lateral_movement", "persistence",
    "exfiltration", "impact", "privilege_escalation",
}


class ExtractedEvent(BaseModel):
    """LLM 提取的事件 (结构化输出)"""
    event_time: str
    event_type: str
    event_category: EventCategory
    title: str
    description: str = ""
    severity: SeverityLevel = "medium"
    attack_techniques: list[str] = Field(default_factory=list)
    source_evidence: str = ""
    confidence: float = 0.7


class TimelineBuilder(AgentBase):
    name = "timeline_builder"

    async def run(self, state: TracebackState) -> dict[str, Any]:
        task_id = state["task_id"]
        logger.info("[{}] 时间线构建开始", task_id)

        # 1. 加载证据
        async with session_scope() as session:
            refs = await evidence_repo.list_evidence(session, task_id)
            full: list[dict[str, Any]] = []
            for ref in refs:
                ev = await evidence_repo.get_evidence(session, ref["evidence_id"])
                full.append(ev or ref)

        # 2. 提取事件
        events = await self._extract_events(task_id, full)
        # 3. 去重 + 排序
        events = self._dedup_sort(events)
        # 4. 关键节点标记
        self._mark_key_events(events)
        logger.info("[{}] 时间线事件 {} 条, 关键 {} 条",
                    task_id, len(events), sum(1 for e in events if e["is_key_event"]))

        # 5. 写入 Mongo
        await timeline_repo.save_events(task_id, events)

        # 6. 推送 Timesketch (可用时)
        await self._push_timesketch(task_id, events)

        return {"stage": "timeline_built", "timeline_events": events}

    async def _extract_events(
        self, task_id: str, evidence: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """LLM 主路径(有 key) / 规则兜底(无 key 或失败)"""
        if settings.deepseek_api_key and evidence:
            try:
                return await self._extract_with_llm(task_id, evidence)
            except Exception as e:  # noqa: BLE001
                logger.warning("[{}] LLM 事件提取失败, 降级规则: {}", task_id, str(e)[:120])
        return self._extract_rule_based(evidence)

    async def _extract_with_llm(
        self, task_id: str, evidence: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """map-reduce: 分批提取事件"""
        events: list[dict[str, Any]] = []
        batch = settings.llm_batch_size
        for i in range(0, len(evidence), batch):
            chunk = evidence[i : i + batch]
            serialized = [
                {"evidence_id": e.get("evidence_id"), "type": e.get("evidence_type"),
                 "source": e.get("source"), "data": _truncate(e.get("parsed_data") or e.get("raw_summary") or {})}
                for e in chunk
            ]
            import json
            system = (
                "你是安全分析师。从证据中提取安全相关事件, 输出 JSON 数组。"
                "event_category 须为: initial_access/execution/persistence/privilege_escalation/"
                "defense_evasion/credential_access/discovery/lateral_movement/collection/"
                "command_and_control/exfiltration/impact/unknown。"
                "attack_techniques 用 MITRE ATT&CK ID(如 T1566.001)。"
            )
            raw = await self.llm.complete(system=system, user=json.dumps(serialized, ensure_ascii=False, default=str))
            for d in _parse_array(raw):
                events.append(self._to_event_dict(task_id, d.get("source_evidence", ""), d))
        return events

    def _extract_rule_based(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """规则兜底: 从结构化/mock 证据提取事件"""
        events: list[dict[str, Any]] = []
        for ev in evidence:
            parsed = ev.get("parsed_data") or {}
            raw = ev.get("parsed_data") or _maybe_dict(ev.get("raw_summary")) or {}
            eid = ev.get("evidence_id", "")
            etype = ev.get("evidence_type", "")

            # 优先用 parsed 里的 event/timestamp; 否则从 raw_summary 解析
            event_name = parsed.get("event") or raw.get("event")
            ts = parsed.get("ts") or parsed.get("timestamp") or raw.get("ts") or raw.get("first_seen")

            if event_name and event_name in _EVENT_MAP:
                category, techniques, severity = _EVENT_MAP[event_name]
                events.append(self._make_event(
                    task_id=ev.get("task_id", ""), evidence_id=eid, ts=ts,
                    event_type=event_name, category=category,
                    title=event_name, techniques=techniques, severity=severity,
                    assets=_extract_assets(parsed, raw),
                ))
            elif etype == "intel" and (parsed.get("malicious") or raw.get("malicious")):
                # 恶意 IOC 命中 → 作为 command_and_control 旁证
                events.append(self._make_event(
                    task_id=ev.get("task_id", ""), evidence_id=eid, ts=ts,
                    event_type="malicious_ioc_hit", category="command_and_control",
                    title=f"恶意IOC命中: {parsed.get('value') or raw.get('value')}",
                    techniques=[], severity="high", assets=[],
                ))
            elif etype == "dns":
                domain = parsed.get("domain") or raw.get("domain")
                if domain:
                    events.append(self._make_event(
                        task_id=ev.get("task_id", ""), evidence_id=eid, ts=ts,
                        event_type="dns_query", category="command_and_control",
                        title=f"DNS查询: {domain}", techniques=[], severity="low", assets=[],
                    ))
        return events

    def _make_event(
        self, *, task_id: str, evidence_id: str, ts: Any, event_type: str,
        category: EventCategory, title: str, techniques: list[str],
        severity: SeverityLevel, assets: list[str],
    ) -> dict[str, Any]:
        return {
            "event_id": f"evt-{uuid.uuid4().hex[:10]}",
            "task_id": task_id,
            "event_time": _parse_time(ts),
            "event_type": event_type,
            "event_category": category,
            "title": title,
            "description": "",
            "severity": severity,
            "source_evidence": [evidence_id] if evidence_id else [],
            "related_events": [],
            "attack_tactics": [category] if category != "unknown" else [],
            "attack_techniques": techniques,
            "is_key_event": False,  # 由 _mark_key_events 设置
            "confidence": 0.8 if techniques else 0.5,
            "verified": False,
            "related_assets": assets,
        }

    def _to_event_dict(self, task_id: str, evidence_id: str, d: dict[str, Any]) -> dict[str, Any]:
        from src.app.traceback.services.mitre import validate_ttp
        techniques = d.get("attack_techniques") or []
        validated = []
        for t in techniques:
            tid, _, _ = validate_ttp(t, 1.0)
            if tid:
                validated.append(tid)
        cat = d.get("event_category", "unknown")
        return {
            "event_id": f"evt-{uuid.uuid4().hex[:10]}",
            "task_id": task_id,
            "event_time": _parse_time(d.get("event_time")),
            "event_type": d.get("event_type", "unknown"),
            "event_category": cat,
            "title": d.get("title", d.get("event_type", "事件")),
            "description": d.get("description", ""),
            "severity": d.get("severity", "medium"),
            "source_evidence": [evidence_id] if evidence_id else [],
            "related_events": [],
            "attack_tactics": [cat] if cat != "unknown" else [],
            "attack_techniques": validated,
            "is_key_event": False,
            "confidence": d.get("confidence", 0.7),
            "verified": False,
        }

    def _dedup_sort(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for e in events:
            key = f"{e.get('event_time')}|{e.get('title')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        out.sort(key=lambda e: e.get("event_time") or datetime.max)
        return out

    def _mark_key_events(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            if e.get("event_category") in _KEY_TACTICS:
                e["is_key_event"] = True

    async def _push_timesketch(self, task_id: str, events: list[dict[str, Any]]) -> None:
        manager = get_manager()
        ts = manager.get("timesketch")
        if ts is None:
            return
        try:
            await ts.create_timeline(f"溯源任务-{task_id}", events)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            logger.warning("[{}] Timesketch 推送失败, 跳过: {}", task_id, str(e)[:100])


def _parse_time(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now()


def _truncate(obj: Any, limit: int = 800) -> Any:
    s = str(obj)
    return s[:limit] if len(s) > limit else obj


def _maybe_dict(s: Any) -> dict[str, Any]:
    if isinstance(s, dict):
        return s
    return {}


def _extract_assets(*sources: dict[str, Any]) -> list[str]:
    assets: list[str] = []
    for s in sources:
        for f in ("src", "dst", "source.ip", "destination.ip", "host", "value"):
            v = s.get(f) if isinstance(s, dict) else None
            if v and isinstance(v, str):
                assets.append(v)
    return assets


def _parse_array(text: str) -> list[dict[str, Any]]:
    import json
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []
