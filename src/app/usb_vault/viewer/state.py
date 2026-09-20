"""查看器纯逻辑: 有效期检查与打开次数软限制 (可单测, 不依赖 Tk)。

打开次数的性质 (README 已声明): 盘只读, 计数只能存收件人机器
(Windows: %LOCALAPPDATA%; macOS: ~/Library/Application Support)
—— 按机计数、可删、换机清零, 纯软约束。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path


def default_state_base() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local)
    return Path.home() / "AppData" / "Local"


def state_path(nonce_hex: str, base: Path | None = None) -> Path:
    base = base or default_state_base()
    return base / "usb_vault" / nonce_hex / "state.json"


def check_expiry(expire: str | None, today: date | None = None) -> bool:
    """已过期返回 True; expire 为空表示永久有效。"""
    if expire is None:
        return False
    return date.fromisoformat(expire) < (today or date.today())


@dataclass(slots=True)
class SoftLimitState:
    """打开次数软限制: 坏文件/无文件一律视为 0 次, 绝不因状态问题挡阅读。"""

    nonce_hex: str
    base_dir: Path | None = None

    def _path(self) -> Path:
        return state_path(self.nonce_hex, self.base_dir)

    def load_opens(self) -> int:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            return int(data["opens"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def record_open(self) -> int:
        """记录一次打开, 返回累计次数; 写入失败静默 (软限制不挡阅读)。"""
        opens = self.load_opens() + 1
        try:
            p = self._path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps({"opens": opens}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        return opens

    def remaining(self, max_opens: int | None) -> int | None:
        """剩余次数; max_opens 为 None 表示不限。"""
        if max_opens is None:
            return None
        return max(0, max_opens - self.load_opens())
