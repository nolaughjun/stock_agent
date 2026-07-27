"""组件统一管理器

对齐 traceback_system_prd.md §9.16.4 ComponentManager
- 注册/初始化/健康检查/降级
- 智能体通过 manager.get(name) 获取适配器, 无需关心具体实现
"""
from __future__ import annotations

import asyncio
from typing import Optional

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.core.logging import logger


class ComponentManager:
    """开源组件/数据源统一管理器"""

    def __init__(self) -> None:
        self._adapters: dict[str, BaseAdapter] = {}
        self._registered: dict[str, BaseAdapter] = {}

    def register(self, name: str, adapter: BaseAdapter) -> None:
        """注册适配器(尚未连接)"""
        self._registered[name] = adapter
        logger.info("已注册适配器: {}", name)

    async def initialize(self) -> dict[str, bool]:
        """并行初始化所有注册的适配器, 单个失败不影响其他 (§7.3 降级)"""
        results: dict[str, bool] = {}
        for name, adapter in self._registered.items():
            ok = await self._init_one(name, adapter)
            results[name] = ok
        healthy = [n for n, ok in results.items() if ok]
        logger.info("适配器初始化完成, 健康: {}", healthy)
        return results

    async def _init_one(self, name: str, adapter: BaseAdapter) -> bool:
        try:
            connected = await adapter.connect()
            if connected:
                healthy = await adapter.health_check()
                if healthy:
                    self._adapters[name] = adapter
                    return True
                logger.warning("适配器 {} 健康检查失败, 降级", name)
                return False
            logger.warning("适配器 {} 连接失败, 降级", name)
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("适配器 {} 初始化异常: {} — 降级", name, str(e)[:150])
            return False

    def get(self, name: str) -> Optional[BaseAdapter]:
        """获取已初始化且健康的适配器, 不可用时返回 None"""
        adapter = self._adapters.get(name)
        if adapter is None or not adapter.available:
            return None
        return adapter

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    async def health_check_all(self) -> dict[str, bool]:
        return {n: await a.health_check() for n, a in self._adapters.items()}

    async def close_all(self) -> None:
        await asyncio.gather(
            *(a.close() for a in self._adapters.values()), return_exceptions=True
        )
        self._adapters.clear()
        self._registered.clear()


# 全局单例
_manager: Optional[ComponentManager] = None


def get_manager() -> ComponentManager:
    global _manager
    if _manager is None:
        _manager = ComponentManager()
    return _manager


def set_manager(m: ComponentManager) -> None:
    """测试注入"""
    global _manager
    _manager = m
