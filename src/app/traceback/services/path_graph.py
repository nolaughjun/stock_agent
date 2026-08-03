"""攻击路径图 SVG 渲染 (纯 Python, 无 graphviz 依赖)

对齐 01_phase1_design.md §4.3 攻击路径图: 节点=资产, 边=攻击动作。
布局: 受害资产按横向移动顺序横向排列, 攻击者节点置左。
"""
from __future__ import annotations

from typing import Any


def render_path_graph_svg(path_graph: dict[str, Any]) -> str:
    """渲染攻击路径图为内嵌 SVG 字符串 (供报告嵌入)。

    Args:
        path_graph: {"nodes": [{"id","type"}], "edges": [{"from","to","technique","label"}]}
    """
    nodes = path_graph.get("nodes") or []
    edges = path_graph.get("edges") or []
    if not nodes:
        return "<p><em>无攻击路径数据</em></p>"

    # 布局: 攻击者在最左(x=40), 受害资产等距横向排列
    attackers = [n for n in nodes if n.get("type") == "attacker"]
    victims = [n for n in nodes if n.get("type") != "attacker"]

    pos: dict[str, tuple[float, float]] = {}
    x = 40.0
    for a in attackers:
        pos[a["id"]] = (x, 110)
        x += 130
    victim_start_x = max(x, 200)
    spacing = 150
    for i, v in enumerate(victims):
        pos[v["id"]] = (victim_start_x + i * spacing, 110)

    width = max((p[0] for p in pos.values()), default=200) + 120
    height = 200

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height}" '
        f'viewBox="0 0 {width:.0f} {height}" style="font-family:sans-serif;font-size:12px">',
    ]
    # 边 (先画, 在节点下层)
    for e in edges:
        src = pos.get(e["from"])
        dst = pos.get(e["to"])
        if not src or not dst:
            continue
        parts.append(
            f'<line x1="{src[0]+50:.0f}" y1="{src[1]}" x2="{dst[0]:.0f}" y2="{dst[1]}" '
            f'stroke="#666" stroke-width="2" marker-end="url(#arrow)"/>'
        )
        midx = (src[0] + 50 + dst[0]) / 2
        label = e.get("label") or e.get("technique") or ""
        if label:
            parts.append(
                f'<text x="{midx:.0f}" y="{src[1]-8}" text-anchor="middle" fill="#444">'
                f'{_escape(label)[:24]}</text>'
            )
    # 节点
    for n in nodes:
        x_n, y_n = pos.get(n["id"], (0, 0))
        color = "#d9534f" if n.get("type") == "attacker" else "#337ab7"
        parts.append(
            f'<rect x="{x_n:.0f}" y="{y_n-22}" width="100" height="44" rx="6" '
            f'fill="{color}" opacity="0.15" stroke="{color}" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{x_n+50:.0f}" y="{y_n-4}" text-anchor="middle" fill="{color}">'
            f'{_escape(n["id"])[:16]}</text>'
        )
        parts.append(
            f'<text x="{x_n+50:.0f}" y="{y_n+12}" text-anchor="middle" fill="#888" '
            f'font-size="10">{n.get("type","victim")}</text>'
        )
    parts.append(
        '<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" '
        'orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#666"/>'
        '</marker></defs>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
