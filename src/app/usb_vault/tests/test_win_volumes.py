"""win_volumes 数据卷候选序单测 (v2.4.0 反转布局的 Windows 寻址核心)。

_list_volumes 全 fake (tmp 目录冒充卷根; GUID 根与盘符根在 pathlib 层无差
别), 真机枚举的 ctypes 细节由 2026-09-22 探针定案、F: 棒复测兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.app.usb_vault import config, win_volumes
from src.app.usb_vault.win_volumes import data_volume_candidates

KEYINFO = config.KEYINFO_NAME


def test_own_volume_with_keyinfo_short_circuits(tmp_path: Path) -> None:
    """单分区盘: 载体就在自身卷 → 只有 [自身], 全机卷扫描一次都不跑。"""
    own = tmp_path / "P1"
    own.mkdir()
    (own / KEYINFO).write_bytes(b"KI")

    def boom() -> list[win_volumes.VolumeInfo]:
        raise AssertionError("单分区盘不应触发全机卷枚举")

    assert data_volume_candidates(own, list_volumes=boom) == [own]


def test_own_without_keyinfo_scans_no_letter_then_lettered(tmp_path: Path) -> None:
    """反转布局: 自身卷无载体 → 无盘符卷 (GUID 根) 优先, 带盘符卷殿后;
    无载体卷与自身卷的 GUID 形态都不进候选。"""
    own = tmp_path / "P1"
    own.mkdir()  # 阅览器分区, 无 keyinfo
    own_letter = win_volumes._drive_spec(own)
    assert own_letter is not None  # tmp 在真实盘符下

    hidden_chain = tmp_path / "vol_hidden"  # 无盘符分区2 (反转布局正主)
    hidden_chain.mkdir()
    (hidden_chain / KEYINFO).write_bytes(b"KI")
    empty_no_letter = tmp_path / "vol_empty"  # 无关无盘符卷 (无载体)
    empty_no_letter.mkdir()
    lettered_chain = tmp_path / "vol_lettered"  # 收件人机补了盘符的数据卷
    lettered_chain.mkdir()
    (lettered_chain / KEYINFO).write_bytes(b"KI")

    volumes: list[win_volumes.VolumeInfo] = [
        (str(empty_no_letter) + "\\", []),  # 无载体: 跳过
        (str(hidden_chain) + "\\", []),  # 无盘符 + 有载体: GUID 根入候选
        (str(lettered_chain) + "\\", ["X:\\"]),  # 有载体: 盘符形态殿后兜底
        (str(own) + "\\", [f"{own_letter}\\"]),  # 自身卷 GUID 形态: 排除
    ]

    cands = data_volume_candidates(own, list_volumes=lambda: volumes)
    assert cands == [Path(str(hidden_chain) + "\\"), Path("X:\\")]


def test_enumeration_failure_returns_empty(tmp_path: Path) -> None:
    """枚举整体失败 (OSError): 返回空候选, 门禁按"非资料盘"给可见报错。"""
    own = tmp_path / "P1"
    own.mkdir()

    def broken() -> list[win_volumes.VolumeInfo]:
        raise OSError("FindFirstVolumeW 失败")

    assert data_volume_candidates(own, list_volumes=broken) == []


def test_non_win32_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data_volume_candidates(tmp_path) == []


def test_drive_spec() -> None:
    assert win_volumes._drive_spec(Path("E:/")) == "E:"
    assert win_volumes._drive_spec(Path("e:/foo/bar")) == "E:"
    assert win_volumes._drive_spec(Path("\\\\?\\Volume{123}\\DATA")) is None
    assert win_volumes._drive_spec(Path("/Volumes/UDISK")) is None
