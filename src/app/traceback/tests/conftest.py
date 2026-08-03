"""traceback 测试公共配置

强制禁用 LLM (走规则兜底), 保证单测确定性、不触网。
真实 LLM 行为由独立评测集(S4+)覆盖。
"""
import pytest

from src.app.traceback.core.config import settings


@pytest.fixture(scope="session", autouse=True)
def _disable_llm():
    settings.deepseek_api_key = None
    yield
