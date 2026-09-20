"""SEED 管理: 构建期生成的 64B 随机数, 是整套密钥体系的根。

- 烧录端: 存放于 src/app/usb_vault/secrets/seed.bin (gitignore)
- viewer exe: PyInstaller 打包时作为 datas 嵌入, 运行期从 sys._MEIPASS 读取

SEED 丢失 => 旧盘仍可用, 但无法再烧录与旧 exe 兼容的新盘 (README 有红字警告)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from src.app.usb_vault.config import SEED_PATH, SEED_SIZE


class SeedError(Exception):
    """SEED 缺失、长度错误或不可读。"""


def create_seed(path: Path = SEED_PATH, *, replace: bool = False) -> bytes:
    """生成新 SEED 并独占写入, 写后回读校验 (根密钥, 不容忍静默损坏)。"""
    last_err: Exception | None = None
    for _attempt in range(2):
        data = os.urandom(SEED_SIZE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # O_BINARY 必须显式加: Windows 上 os.open 不带它默认走 CRT 文本模式,
            # 每个 0x0A 会被翻译成 0x0D 0x0A, 64B 随机数约 22% 概率含 0x0A 即损坏
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0) \
                | (0 if replace else os.O_EXCL)
            fd = os.open(path, flags, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            if path.read_bytes() == data:
                return data
            last_err = OSError(f"回读校验失败: 文件大小 {path.stat().st_size}")
            path.unlink(missing_ok=True)  # 坏种子立即清掉, 下轮重写
        except OSError as exc:
            last_err = exc
    raise SeedError(f"SEED 写入失败: {path}: {last_err}")


def load_seed(path: Path = SEED_PATH) -> bytes:
    """读取并校验 SEED; 不存在或长度不符时抛 SeedError。"""
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise SeedError(f"SEED 不存在: {path} (先运行 new-seed 或 build-viewer 生成)") from exc
    except OSError as exc:
        raise SeedError(f"SEED 不可读: {path}: {exc}") from exc
    if len(data) != SEED_SIZE:
        raise SeedError(f"SEED 长度错误: 期望 {SEED_SIZE} 字节, 实际 {len(data)} ({path})")
    return data


def get_or_create_seed(path: Path = SEED_PATH) -> bytes:
    """烧录/构建前取 SEED: 存在则读, 不存在则建。"""
    if path.exists():
        return load_seed(path)
    return create_seed(path)


def load_embedded_seed() -> bytes:
    """viewer 运行期读 SEED: 打包后从 _MEIPASS, 开发态从 secrets/ 目录。"""
    if getattr(sys, "frozen", False):
        candidate = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "seed.bin"
        return load_seed(candidate)
    return load_seed()
