"""selfcheck 单测: --selfcheck 自检的判定与输出 (2026-09-18 产物自证机制)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.app.usb_vault import config, mac_volume
from src.app.usb_vault.viewer import selfcheck


def test_selfcheck_green_on_darwin_with_volume(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """macOS 直读成功: 全 ✔, 退出码 0, 版本与序列号都在输出里。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "src.app.usb_vault.viewer.app._mac_data_volume_candidates",
        lambda own: [Path("/Volumes/TEST1")],
    )
    monkeypatch.setattr(mac_volume, "get_volume_serial_mac",
                        lambda root: 0xBCBB1A98)
    assert selfcheck.run_selfcheck(Path("/Volumes/P2")) == 0
    out = capsys.readouterr().out
    assert config.VIEWER_VERSION in out
    assert "0xBCBB1A98" in out
    assert "自检通过" in out


def test_selfcheck_red_without_volume(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """数据卷找不到: ✗ + 退出码 1 (产物链断在哪一环, 一眼可见)。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "src.app.usb_vault.viewer.app._mac_data_volume_candidates",
        lambda own: [],
    )
    assert selfcheck.run_selfcheck(Path("/Volumes/P2")) == 1
    assert "数据卷未找到" in capsys.readouterr().out


def test_selfcheck_reports_channel_fallback(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """直读被权限墙挡住 + UUID 不合成: 文案指向授权框兜底, 仍算绿 (流程有后手)。"""
    from src.app.usb_vault.volume_id import VolumeIdError

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "src.app.usb_vault.viewer.app._mac_data_volume_candidates",
        lambda own: [Path("/Volumes/TEST1")],
    )

    def denied(root: str | Path) -> int:
        raise VolumeIdError("读取U盘引导扇区权限不足")

    monkeypatch.setattr(mac_volume, "get_volume_serial_mac", denied)
    monkeypatch.setattr(mac_volume, "_diskutil_volume_uuid",
                        lambda root: (None, "没有 VolumeUUID 键"))
    assert selfcheck.run_selfcheck(Path("/Volumes/P2")) == 0
    assert "授权框兜底" in capsys.readouterr().out
