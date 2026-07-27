"""智能体基类

对齐 01_phase1_design.md §3.3
约定: run() 返回状态增量(partial state dict), 键须为 TracebackState 子集,
LangGraph 做 reducer 合并。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.app.traceback.core.llm import LLMClient, get_llm
from src.app.traceback.core.logging import logger


class AgentBase(ABC):
    """智能体抽象基类"""

    name: str = "base"

    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or get_llm()

    @abstractmethod
    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """执行智能体逻辑, 返回状态增量。

        Args:
            state: 当前全局状态 (TracebackState)
        Returns:
            partial state, 由 LangGraph reducer 合并
        """
        ...

    async def safe_run(self, state: dict[str, Any]) -> dict[str, Any]:
        """带异常兜底的执行, 错误写入 state['errors'] 不中断整体流程。"""
        try:
            return await self.run(state)
        except Exception as e:  # noqa: BLE001
            logger.exception("智能体 {} 执行失败: {}", self.name, str(e)[:200])
            return {"errors": [f"{self.name}: {type(e).__name__}: {str(e)[:200]}"]}
