"""LLM 客户端封装

对齐 01_phase1_design.md §8
- 统一封装 DeepSeek (OpenAI 兼容) 调用
- 结构化输出优先: 用 JSON schema 强约束 + 校验失败重试
- 防注入: 原始数据作为 user message 引用块, 系统提示固定
"""
from __future__ import annotations

import json
from typing import Any, Optional, Type

from pydantic import BaseModel, ValidationError

from src.app.traceback.core.config import settings
from src.app.traceback.core.logging import logger

# langchain_openai 已安装, 用其 ChatOpenAI 走 OpenAI 兼容协议接 DeepSeek
from langchain_openai import ChatOpenAI  # type: ignore


class LLMClient:
    """大模型调用统一入口"""

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.model = model or settings.deepseek_model
        self.temperature = temperature if temperature is not None else settings.llm_temperature
        self.max_retries = settings.llm_max_retries
        self._client = ChatOpenAI(
            model=self.model,
            temperature=self.temperature,
            base_url=base_url or settings.deepseek_base_url,
            api_key=api_key or settings.deepseek_api_key or "not-set",
            request_timeout=settings.llm_request_timeout,
        )

    async def complete(self, system: str, user: str) -> str:
        """纯文本补全"""
        from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore

        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        resp = await self._client.ainvoke(messages)
        return resp.content if isinstance(resp.content, str) else str(resp.content)

    async def complete_json(
        self,
        system: str,
        user: str,
        schema_model: Type[BaseModel],
    ) -> BaseModel:
        """结构化 JSON 输出, 强约束为 schema_model, 校验失败重试。

        对齐 §8.2 结构化输出优先 + 校验失败重试(最多 max_retries 次)。
        """
        from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore

        schema_str = json.dumps(
            schema_model.model_json_schema(), ensure_ascii=False, indent=2
        )
        sys_msg = SystemMessage(
            content=(
                f"{system}\n\n"
                "【输出要求】仅输出符合下面 JSON Schema 的 JSON, 不要任何解释或 markdown 代码块:\n"
                f"{schema_str}"
            )
        )
        last_err: Optional[Exception] = None
        last_raw: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.ainvoke([sys_msg, HumanMessage(content=user)])
                raw = resp.content if isinstance(resp.content, str) else str(resp.content)
                last_raw = raw
                parsed = _extract_json(raw)
                return schema_model.model_validate(parsed)
            except (ValidationError, json.JSONDecodeError) as e:
                last_err = e
                logger.warning(
                    "LLM JSON 校验失败 attempt={}/{} err={}",
                    attempt + 1,
                    self.max_retries + 1,
                    str(e)[:200],
                )
                user = (
                    f"{user}\n\n"
                    f"上次输出未通过校验: {str(e)[:300]}\n请修正后重新输出合法 JSON。"
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.error("LLM 调用异常: {}", str(e)[:200])
                raise

        raise RuntimeError(f"LLM 结构化输出校验失败, 重试耗尽: {last_err}") from last_err


def _extract_json(text: str) -> Any:
    """从可能含 markdown 代码块的文本中提取 JSON"""
    text = text.strip()
    if text.startswith("```"):
        # 去掉 ```json ... ```
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


# 单例 (懒加载, 便于测试替换)
_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm


def set_llm(client: LLMClient) -> None:
    """测试注入"""
    global _llm
    _llm = client
