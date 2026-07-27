"""日志工具

遵循项目既有约定: loguru (参考 investment_research/utils/logger.py)
01_phase1_design.md §10 提及 structlog, 实际沿用项目 loguru 约定保持一致。
"""
from __future__ import annotations

import sys

from loguru import logger

logger.remove()
logger.add(
    sys.stdout,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
    level="DEBUG",
)

logger.add(
    "src/app/traceback/logs/app_{time}.log",
    rotation="1 day",
    retention="14 days",
    level="INFO",
    encoding="utf-8",
)

__all__ = ["logger"]
