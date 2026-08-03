"""报告生成服务

对齐 01_phase1_design.md §4.4
- 结构化数据由 Jinja2 模板渲染, 不交 LLM (避免幻觉)
- LLM 仅生成章节叙事文本(事件概述/攻击链叙述/处置建议)
- 格式转换: MD→HTML→PDF(weasyprint), MD→Word(pypandoc, 可选)
- 存储到 MinIO reports bucket
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.app.traceback.core.config import MODULE_DIR, settings
from src.app.traceback.core.logging import logger

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
    attack_narrative: str = "",
    response_suggestions: str = "",
    path_graph_svg: str = "",
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
        attack_narrative=attack_narrative,
        response_suggestions=response_suggestions,
        path_graph_svg=path_graph_svg,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return md


def markdown_to_html(md: str, *, path_graph_svg: str = "") -> str:
    """Markdown → HTML (weasyprint 前置), 嵌入 SVG 路径图"""
    import markdown as md_lib  # type: ignore

    html = md_lib.markdown(md, extensions=["tables", "fenced_code"])
    if path_graph_svg:
        # 替换模板中的占位标记
        html = html.replace("<!--PATH_GRAPH-->", path_graph_svg)
    style = (
        "<style>body{font-family:'Microsoft YaHei',Arial,sans-serif;margin:30px}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:6px}"
        "th{background:#f0f0f0}h1{color:#337ab7}svg{max-width:100%}</style>"
    )
    return f"<!DOCTYPE html><html><head><meta charset='utf-8'>{style}</head><body>{html}</body></html>"


def markdown_to_pdf(md: str, out_path: Any, *, path_graph_svg: str = "") -> str:
    """MD → PDF (weasyprint)。out_path 可为 str 或 Path。"""
    from pathlib import Path

    from weasyprint import HTML  # type: ignore

    html = markdown_to_html(md, path_graph_svg=path_graph_svg)
    out = Path(out_path) if not isinstance(out_path, Path) else out_path
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(out))
    return str(out)


def markdown_to_word(md: str, out_path: Any) -> str | None:
    """MD → Word (pypandoc, 需系统装 pandoc)。失败返回 None。"""
    from pathlib import Path

    try:
        import pypandoc  # type: ignore

        out = Path(out_path) if not isinstance(out_path, Path) else out_path
        out.parent.mkdir(parents=True, exist_ok=True)
        pypandoc.convert_text(md, "docx", format="md", outputfile=str(out))
        return str(out)
    except Exception as e:  # noqa: BLE001
        logger.warning("Word 导出失败(需系统安装 pandoc): {}", str(e)[:100])
        return None


def save_report(task_id: str, content: str | bytes, fmt: str = "md") -> str:
    """保存报告到本地(outputs/reports)。返回本地路径。S4 主路径存 MinIO。"""
    out_dir = MODULE_DIR / "outputs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{task_id}.{fmt}"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return str(path)


# ---- MinIO 存储 ----

def minio_report_key(task_id: str, fmt: str) -> str:
    return f"{task_id}/report.{fmt}"


async def upload_report_to_minio(task_id: str, content: bytes, fmt: str) -> str:
    """上传报告到 MinIO reports bucket, 返回 object key"""
    from io import BytesIO

    from src.app.traceback.core.db import get_minio_client

    key = minio_report_key(task_id, fmt)
    bucket = settings.bucket_reports
    client = get_minio_client()
    await asyncio.to_thread(_ensure_bucket, client, bucket)
    ct = {"md": "text/markdown", "pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[fmt]
    await asyncio.to_thread(
        client.put_object, bucket, key, BytesIO(content), len(content), ct
    )
    return key


def _ensure_bucket(client, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


async def get_report_from_minio(task_id: str, fmt: str) -> bytes | None:
    """从 MinIO 读取报告, 不存在返回 None"""
    from src.app.traceback.core.db import get_minio_client


    key = minio_report_key(task_id, fmt)
    bucket = settings.bucket_reports
    client = get_minio_client()
    try:
        obj = await asyncio.to_thread(client.get_object, bucket, key)
        try:
            data = await asyncio.to_thread(lambda: obj.read())
        finally:
            obj.close()
            obj.release_conn()
        return data
    except Exception:  # noqa: BLE001
        return None
