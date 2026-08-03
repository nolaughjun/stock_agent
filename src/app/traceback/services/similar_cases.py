"""相似案例匹配 (P2 基础版)

对齐 02_phase2_design.md §3.4
- P2: 预置种子案例 + 内存相似度(TTP 重叠为主), 无外部依赖
- P3: 案例库建成后替换为 Milvus 向量检索 (接口不变)
"""
from __future__ import annotations

import json
from typing import Any

from src.app.traceback.core.config import MODULE_DIR
from src.app.traceback.core.logging import logger
from src.app.traceback.schemas.attribution import ActorFeatures

_SEED_FILE = MODULE_DIR / "data" / "seed_cases.json"


class SimilarCaseMatcher:
    """相似历史案例匹配器"""

    def __init__(self) -> None:
        self._cases: list[dict[str, Any]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        if self._cases is None:
            self._cases = json.loads(_SEED_FILE.read_text(encoding="utf-8"))["cases"]
        return self._cases

    async def search(
        self, features: ActorFeatures, attack_type: str = "", top_k: int = 3
    ) -> list[dict[str, Any]]:
        """检索 Top-K 相似案例。

        相似度 = TTP Jaccard * 0.6 + 同攻击类型 * 0.3 + IOC 类型重叠 * 0.1
        """
        cases = self._load()
        event_ttps = set(features.ttps)
        scored: list[tuple[float, dict]] = []
        for c in cases:
            case_ttps = set(c.get("ttps") or [])
            if not event_ttps:
                ttp_sim = 0.0
            else:
                ttp_sim = len(event_ttps & case_ttps) / len(event_ttps)
            type_sim = 1.0 if attack_type and c.get("attack_type") == attack_type else 0.0
            score = 0.6 * ttp_sim + 0.3 * type_sim
            scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, c in scored[:top_k]:
            if score <= 0:
                continue
            results.append({
                "case_id": c["case_id"], "title": c["title"],
                "attack_type": c.get("attack_type"), "similarity": round(score, 3),
                "threat_actor": c.get("threat_actor"),
                "matched_ttps": sorted(event_ttps & set(c.get("ttps") or [])),
            })
        logger.info("相似案例匹配: 输入 {} TTPs, 命中 {} 个案例", len(event_ttps), len(results))
        return results
