"""溯源分析系统配置管理

对齐 01_phase1_design.md §2.1 / §8.1
遵循项目既有约定: pydantic_settings + .env (参考 investment_research/config.py)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# 模块根目录: src/app/traceback/
MODULE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置"""

    # 应用信息
    app_name: str = "溯源分析系统"
    app_version: str = "0.1.0"
    debug: bool = True

    # ---- 基础设施连接 ----
    # P1 默认端口对齐 docker-compose.yml (5xxxx 避免与本机冲突)
    postgres_dsn: str = "postgresql+asyncpg://traceback:traceback_dev@localhost:55432/traceback"
    mongo_dsn: str = "mongodb://traceback:traceback_dev@localhost:57017/?authSource=admin"
    mongo_db: str = "traceback"
    redis_url: str = "redis://localhost:56379/0"
    minio_endpoint: str = "localhost:59000"
    minio_access_key: str = "traceback"
    minio_secret_key: str = "traceback_dev"
    minio_secure: bool = False

    # bucket
    bucket_evidence_raw: str = "evidence-raw"
    bucket_reports: str = "reports"

    # ---- LLM (DeepSeek, OpenAI 兼容) ----
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: Optional[str] = None
    deepseek_model: str = "deepseek-chat"
    llm_temperature: float = 0.2
    llm_max_retries: int = 2          # JSON schema 校验失败重试次数
    llm_request_timeout: int = 120

    # ---- 适配器 (P1: 数据源, 缺省可空, 用 mock 降级) ----
    siem_base_url: Optional[str] = None        # SIEM/OpenSearch
    siem_api_key: Optional[str] = None
    edr_base_url: Optional[str] = None
    edr_api_key: Optional[str] = None
    dns_base_url: Optional[str] = None
    misp_url: Optional[str] = None
    misp_api_key: Optional[str] = None
    timesketch_url: Optional[str] = None
    timesketch_api_key: Optional[str] = None

    # ---- 分析参数 ----
    evidence_collect_timeout: int = 60          # 单适配器超时(秒)
    max_log_per_task: int = 100_000             # P1 限 10 万日志 (TR-003 对策)
    llm_batch_size: int = 50                    # 证据分批送 LLM
    # 无真实数据源时, 适配器返回 canned mock 证据(勒索病毒场景), 便于 S3/S4 端到端开发
    evidence_mock_mode: bool = True
    # 任务调度: asyncio (默认, 进程内) | celery (需安装 celery + worker)
    task_runner: str = "asyncio"

    # ---- 安全 (P1 基础 Token, P4 上 RBAC/LDAP) ----
    api_token: Optional[str] = None             # 为空则不强制鉴权 (仅 dev)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
