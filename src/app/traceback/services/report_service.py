"""报告生成服务

对齐 01_phase1_design.md §4.4
- 结构化数据由 Jinja2 模板渲染, 不交 LLM, 避免幻觉
- LLM 仅生成各章节叙事文本(事件概述/攻击链叙述/处置建议)
- 格式转换: MD → PDF(weasyprint) / Word(pandoc, S4)
M1: 提供 Markdown 渲染骨架; PDF/Word 转换 S4。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.app.traceback.core.config import MODULE_DIR

_TEMPLATES_DIR = MODULE_DIR / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(),
    enable_async=True,
)


async def render_markdown(
    template_name: str,
    *,
    task: dict[str, Any],
    timeline_events: list[dict[str, Any]] | None = None,
    attack_chain: dict[str, Any] | None = None,
    attribution: dict[str, Any] | None = None,
    overview: str = "",
    response_suggestions: str = "",
) -> str:
    """渲染 Markdown 报告 (结构化数据直渲染, 叙事文本由调用方 LLM 生成后传入)"""
    template = _env.get_template(template_name)
    key_event_count = sum(1 for e in (timeline_events or []) if e.get("is_key_event"))
    md = await template.render_async(
        task=task,
        timeline_events=timeline_events or [],
        key_event_count=key_event_count,
        attack_chain=attack_chain,
        attribution=attribution,
        overview=overview,
        response_suggestions=response_suggestions,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return md


def save_report(task_id: str, content: str, fmt: str = "md") -> str:
    """保存报告到本地(S4 改存 MinIO)。返回本地路径。"""
    out_dir = MODULE_DIR / "outputs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{task_id}.{fmt}"
    path.write_text(content, encoding="utf-8")
    return str(path)


async def markdown_to_pdf(md: str, out_path: Path) -> str:
    """MD → PDF (weasyprint, 需先 MD→HTML)。S4 完善。"""
    import markdown as md_lib  # type: ignore

    html = md_lib.markdown(md, extensions=["tables", "fenced_code"])
    from weasyprint import HTML  # type: ignore

    HTML(string=html).write_pdf(str(out_path))
    return str(out_path)
