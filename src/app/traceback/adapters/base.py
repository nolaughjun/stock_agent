"""适配器基类

对齐 traceback_system_prd.md §9.16.3 + 01_phase1_design.md §7.1
统一封装不同开源组件/数据源接口, 由 ComponentManager 注册、健康检查、降级。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.app.traceback.core.logging import logger


class BaseAdapter(ABC):
    """组件适配器基类"""

    name: str = "base"
    available: bool = True

    @abstractmethod
    async def connect(self) -> bool:
        """建立连接, 返回是否成功"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接"""
        ...

    async def safe_call(self, coro: Any, *, context: str = "") -> Any:
        """带降级的调用包装: 失败标记 unavailable, 不抛出, 返回 None。

        对齐 §7.3 降级策略: 单源失败不中断整体流程。
        """
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            self.available = False
            logger.warning(
                "适配器 {} 调用失败[{}] : {} — 降级",
                self.name, context, str(e)[:200],
            )
            return None
