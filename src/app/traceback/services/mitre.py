"""MITRE ATT&CK 白名单加载与校验

对齐 01_phase1_design.md §4.3 — LLM 输出的 technique_id 须命中白名单,
未命中标记 unknown 并降置信。白名单为策展子集, 可替换为完整 enterprise-attack.json。
"""
from __future__ import annotations

import json
from functools import lru_cache

from src.app.traceback.core.config import MODULE_DIR
from src.app.traceback.core.logging import logger

_WHITELIST_FILE = MODULE_DIR / "data" / "mitre_attack_whitelist.json"


@lru_cache(maxsize=1)
def load_whitelist() -> dict:
    """加载 ATT&CK 白名单 (tactics / techniques)"""
    try:
        data = json.loads(_WHITELIST_FILE.read_text(encoding="utf-8"))
        logger.info("ATT&CK 白名单已加载: {} 技术", len(data.get("techniques", {})))
        return data
    except Exception as e:  # noqa: BLE001
        logger.error("ATT&CK 白名单加载失败: {}", str(e)[:120])
        return {"tactics": [], "techniques": {}}


def is_valid_technique(technique_id: str | None) -> bool:
    """technique_id 是否命中白名单(含子技术)"""
    if not technique_id:
        return False
    data = load_whitelist()
    techs = data.get("techniques", {})
    if technique_id in techs:
        return True
    # 子技术: 父 ID 命中即可 (如 T1021.001 → T1021)
    parent = technique_id.split(".")[0]
    return parent in techs


def tactic_of(technique_id: str | None) -> str | None:
    """technique_id → MITRE 战术"""
    if not technique_id:
        return None
    data = load_whitelist()
    techs = data.get("techniques", {})
    if technique_id in techs:
        return techs[technique_id].get("tactic")
    parent = technique_id.split(".")[0]
    if parent in techs:
        return techs[parent].get("tactic")
    return None


def validate_ttp(technique_id: str | None, confidence: float) -> tuple[str | None, str, float]:
    """校验 LLM 输出的 TTP。

    Returns:
        (technique_id, tactic, adjusted_confidence)
        未命中白名单: technique_id=None, tactic='unknown', 置信度减半
    """
    if is_valid_technique(technique_id):
        return technique_id, tactic_of(technique_id) or "unknown", confidence
    return None, "unknown", confidence * 0.5
