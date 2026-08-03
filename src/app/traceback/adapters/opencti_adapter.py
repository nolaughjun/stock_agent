"""OpenCTI 归因后端适配器

对齐 traceback_system_prd.md §9.16.2-B / 02_phase2_design.md §3.3
- 真实模式: GraphQL 查询威胁行为者 + attackPatterns + campaigns
- 种子降级: 无 OPENCTI_URL 时用 seed_threat_actors.json (Apache-2.0 组件, 深度集成)
License: OpenCTI Apache-2.0, 可深度集成。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.core.config import MODULE_DIR, settings
from src.app.traceback.core.logging import logger

_SEED_FILE = MODULE_DIR / "data" / "seed_threat_actors.json"


class OpenCTIAdapter(BaseAdapter):
    """威胁情报/归因后端 (OpenCTI GraphQL 或种子降级)"""

    name = "opencti"

    def __init__(self) -> None:
        self.url = settings.opencti_url
        self.api_key = settings.opencti_api_key
        self._client: httpx.AsyncClient | None = None
        self._seed_actors: list[dict] | None = None

    async def connect(self) -> bool:
        if self.url:
            self._client = httpx.AsyncClient(
                base_url=self.url,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                timeout=20,
            )
            return True
        # 种子模式始终可用
        return True

    async def health_check(self) -> bool:
        if not self.url:
            return True
        if not self._client:
            return False
        try:
            r = await self._client.post("/graphql", json={"query": "{ settings { platformVersion } }"})
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_actors(self) -> list[dict[str, Any]]:
        """获取所有威胁行为者(含 TTPs/工具/IOC), 供三因子匹配。

        真实模式: GraphQL 查询; 种子模式: 读 seed_threat_actors.json。
        """
        if self.url and self._client:
            try:
                return await self._query_actors_graphql()
            except Exception as e:  # noqa: BLE001
                logger.warning("OpenCTI 查询失败, 降级种子: {}", str(e)[:120])
                self.available = False
        return self._load_seed_actors()

    async def search_by_ioc(self, value: str, ioc_type: str = "ip") -> list[dict[str, Any]]:
        """IOC → 关联威胁行为者"""
        actors = await self.get_actors()
        matched = []
        for a in actors:
            iocs = a.get("iocs") or []
            if value in iocs:
                matched.append(a)
        return matched

    # ---- GraphQL 实现 ----

    async def _query_actors_graphql(self) -> list[dict[str, Any]]:
        query = """
        query {
          threatActors {
            edges {
              node {
                name description sophistication
                x_mitre_id
                primaryMotivation
                goals { edges { node { value } } }
                attackPatterns { edges { node { x_mitre_id name } } }
              }
            }
          }
        }"""
        r = await self._client.post("/graphql", json={"query": query})  # type: ignore[union-attr]
        r.raise_for_status()
        data = r.json().get("data", {}).get("threatActors", {}).get("edges", [])
        return [
            {
                "name": n["node"]["name"],
                "description": n["node"].get("description", ""),
                "sophistication": n["node"].get("sophistication", ""),
                "primary_motivation": n["node"].get("primaryMotivation", ""),
                "goals": [g["node"]["value"] for g in n["node"].get("goals", {}).get("edges", [])],
                "ttps": [ap["node"].get("x_mitre_id") for ap in n["node"].get("attackPatterns", {}).get("edges", [])
                         if ap["node"].get("x_mitre_id")],
                "tools": [],
                "iocs": [],
            }
            for n in data
        ]

    def _load_seed_actors(self) -> list[dict[str, Any]]:
        if self._seed_actors is None:
            self._seed_actors = json.loads(_SEED_FILE.read_text(encoding="utf-8"))["actors"]
        return self._seed_actors
