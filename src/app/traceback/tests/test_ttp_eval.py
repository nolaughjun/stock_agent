"""TTP 标注黄金样本评测 (对齐 01_phase1_design.md §11)

质量门: TTP 标注准确率 > 80%。
评测端到端: TimelineBuilder 规则提取 → AttackChainAgent TTP 白名单标注,
对黄金样本计算 precision / recall / F1, 断言达标。
"""
from __future__ import annotations

import json

import pytest

from src.app.traceback.agents.attack_chain import AttackChainAgent
from src.app.traceback.agents.timeline_builder import TimelineBuilder
from src.app.traceback.core.config import MODULE_DIR

_SAMPLES_FILE = MODULE_DIR / "data" / "golden_ttp_samples.json"


def _load_samples() -> list[dict]:
    data = json.loads(_SAMPLES_FILE.read_text(encoding="utf-8"))
    return data["samples"]


def _extract_techniques(evidence: list[dict]) -> set[str]:
    """端到端: 规则提取事件 → TTP 白名单标注 → 有效技术集合"""
    tb = TimelineBuilder.__new__(TimelineBuilder)
    events = tb._extract_rule_based(evidence)
    ac = AttackChainAgent.__new__(AttackChainAgent)
    ttp_summary = ac._annotate_ttp(events)
    # 只取命中白名单的有效 technique_id (None 表示未命中, 不计入正确产出)
    return {t["technique_id"] for t in ttp_summary if t["technique_id"]}


@pytest.mark.parametrize("sample", _load_samples(), ids=lambda s: s["scenario"])
def test_ttp_accuracy_per_sample(sample: dict):
    """每个场景的 TTP 标注 precision/recall 应达标"""
    produced = _extract_techniques(sample["evidence"])
    expected = set(sample["expected_techniques"])

    tp = len(produced & expected)
    fp = len(produced - expected)
    fn = len(expected - produced)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0

    assert precision >= 0.8, (
        f"[{sample['scenario']}] precision={precision:.2f} < 0.8, 误报技术: {produced - expected}"
    )
    assert recall >= 0.8, (
        f"[{sample['scenario']}] recall={recall:.2f} < 0.8, 漏报技术: {expected - produced}"
    )


def test_ttp_overall_accuracy():
    """整体 TTP 标注 F1 > 0.8 (§11 质量门)"""
    total_tp = total_fp = total_fn = 0
    for sample in _load_samples():
        produced = _extract_techniques(sample["evidence"])
        expected = set(sample["expected_techniques"])
        total_tp += len(produced & expected)
        total_fp += len(produced - expected)
        total_fn += len(expected - produced)

    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    assert f1 > 0.8, f"整体 TTP F1={f1:.3f} 未达标 (>0.8), precision={precision:.3f} recall={recall:.3f}"


def test_unknown_events_produce_no_invalid_technique():
    """噪声/未知事件不应产出未命中白名单的 technique (防幻觉)"""
    evidence = [
        {"evidence_type": "log", "source": "siem",
         "parsed_data": {"event": "totally_unknown_event", "ts": "2026-07-04T10:00:00Z", "src": "10.0.0.1"}},
    ]
    produced = _extract_techniques(evidence)
    assert produced == set(), f"未知事件不应产出技术, 实际: {produced}"
