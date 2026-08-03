"""报告生成智能体

对齐 01_phase1_design.md §4.4
流程: 加载结果(task/timeline/attack_chain/attribution) → LLM 叙事(规则兜底)
      → 渲染路径图 SVG → 模板渲染 → MD/PDF 转换 → 存 MinIO
结构化数据模板直渲染(不交 LLM, 避免幻觉), LLM 仅生成章节叙事。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.app.traceback.agents.base import AgentBase
from src.app.traceback.core.config import settings
from src.app.traceback.core.logging import logger
from src.app.traceback.orchestration.state import TracebackState
from src.app.traceback.services import report_service
from src.app.traceback.services.path_graph import render_path_graph_svg


class ReportNarrative(BaseModel):
    """LLM 生成的章节叙事"""
    overview: str = Field("", description="事件概述")
    attack_narrative: str = Field("", description="攻击链叙述")
    response_suggestions: str = Field("", description="处置建议")


class Reporter(AgentBase):
    name = "reporter"

    async def run(self, state: TracebackState) -> dict[str, Any]:
        task_id = state["task_id"]
        task = state.get("task") or {"task_id": task_id, "task_name": "溯源任务"}
        timeline = state.get("timeline_events") or []
        attack_chain = state.get("attack_chain")
        attribution = state.get("attribution")

        logger.info("[{}] 报告生成开始", task_id)

        # 1. LLM 叙事 (规则兜底)
        narrative = await self._gen_narrative(task, timeline, attack_chain, attribution)

        # 2. 路径图 SVG
        svg = ""
        if attack_chain and attack_chain.get("path_graph"):
            svg = render_path_graph_svg(attack_chain["path_graph"])

        # 3. 渲染 Markdown
        md = await report_service.render_markdown(
            "report_detail.md.j2",
            task=task, timeline_events=timeline, attack_chain=attack_chain,
            attribution=attribution, overview=narrative.overview,
            attack_narrative=narrative.attack_narrative,
            response_suggestions=narrative.response_suggestions,
            path_graph_svg=svg,
        )

        # 4. 格式转换 (PDF 用 weasyprint; Word 可选)
        pdf_bytes = self._md_to_pdf_bytes(md, svg)
        report_keys: dict[str, str] = {"md": "", "pdf": ""}

        # 5. 存 MinIO (失败降级本地)
        report_keys["md"] = await self._store(task_id, md.encode("utf-8"), "md")
        if pdf_bytes:
            report_keys["pdf"] = await self._store(task_id, pdf_bytes, "pdf")

        logger.info("[{}] 报告生成完成: {}", task_id, report_keys)
        return {
            "stage": "reported",
            "report_path": report_keys.get("pdf") or report_keys.get("md"),
            "report_formats": report_keys,
        }

    async def _gen_narrative(
        self, task: dict[str, Any], timeline: list[dict[str, Any]],
        attack_chain: dict[str, Any] | None, attribution: dict[str, Any] | None,
    ) -> ReportNarrative:
        if not settings.deepseek_api_key:
            return self._rule_narrative(task, timeline, attack_chain, attribution)
        try:
            return await self.llm.complete_json(  # type: ignore[return-value]
                system=(
                    "你是安全分析师, 撰写专业溯源报告章节。基于结构化结果生成概述、攻击链叙述、处置建议。"
                    "客观陈述事实, 不臆造细节。"
                ),
                user=self._narrative_input(task, timeline, attack_chain, attribution),
                schema_model=ReportNarrative,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[{}] LLM 叙事失败, 降级规则: {}", task.get("task_id"), str(e)[:120])
            return self._rule_narrative(task, timeline, attack_chain, attribution)

    def _narrative_input(
        self, task: dict, timeline: list, attack_chain: dict | None, attribution: dict | None
    ) -> str:
        import json
        return json.dumps({
            "task": {"name": task.get("task_name"), "description": task.get("description")},
            "timeline": [{"time": str(e.get("event_time")), "title": e.get("title"),
                          "category": e.get("event_category")} for e in timeline[:30]],
            "attack_chain": attack_chain,
            "attribution": attribution,
        }, ensure_ascii=False, default=str)

    def _rule_narrative(
        self, task: dict, timeline: list, attack_chain: dict | None, attribution: dict | None
    ) -> ReportNarrative:
        """规则兜底叙事 (无 LLM 时)"""
        name = task.get("task_name", "该事件")
        key_events = [e for e in timeline if e.get("is_key_event")]
        overview = (
            f"本次分析针对「{name}」。共收集证据并构建攻击时间线，"
            f"识别关键事件 {len(key_events)} 个，总事件 {len(timeline)} 个。"
        )
        attack_narrative = "（结构化攻击链见下文时间线与攻击链章节）"
        if attack_chain and attack_chain.get("entry_point"):
            ep = attack_chain["entry_point"]
            attack_narrative = (
                f"攻击者通过 {ep.get('vector','未知向量')} 入侵主机 {ep.get('asset')}，"
                f"随后执行横向移动与凭据获取，最终实施加密破坏。"
            )
        suggestions = self._rule_response(attack_chain)
        return ReportNarrative(
            overview=overview, attack_narrative=attack_narrative, response_suggestions=suggestions
        )

    def _rule_response(self, attack_chain: dict | None) -> str:
        if not attack_chain:
            return "1. 隔离受影响主机\n2. 排查攻击入口\n3. 修复漏洞\n4. 加强监控"
        lines = []
        if attack_chain.get("affected_assets"):
            lines.append(f"1. 立即隔离受影响主机: {', '.join(attack_chain['affected_assets'])}")
        if attack_chain.get("attacker_ips"):
            lines.append(f"2. 在防火墙封禁攻击者 IP: {', '.join(attack_chain['attacker_ips'])}")
        if attack_chain.get("attacker_domains"):
            lines.append(f"3. DNS 层面拦截恶意域名: {', '.join(attack_chain['attacker_domains'])}")
        lines.append(f"{len(lines)+1}. 重置可能泄露的凭据, 排查入口向量")
        lines.append(f"{len(lines)+1}. 恢复备份, 加强终端与流量监控")
        return "\n".join(lines)

    def _md_to_pdf_bytes(self, md: str, svg: str) -> bytes | None:
        import tempfile
        from pathlib import Path

        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            report_service.markdown_to_pdf(md, tmp_path, path_graph_svg=svg)
            data = tmp_path.read_bytes()
            tmp_path.unlink(missing_ok=True)
            return data
        except Exception as e:  # noqa: BLE001
            logger.warning("PDF 转换失败(weasyprint): {}", str(e)[:120])
            return None

    async def _store(self, task_id: str, content: bytes, fmt: str) -> str:
        """存 MinIO, 失败降级本地, 返回标识 (minio:key 或 local:path)"""
        try:
            key = await report_service.upload_report_to_minio(task_id, content, fmt)
            return f"minio:{key}"
        except Exception as e:  # noqa: BLE001
            logger.warning("[{}] MinIO 上传失败, 降级本地: {}", task_id, str(e)[:100])
            local = report_service.save_report(task_id, content, fmt)
            return f"local:{local}"
