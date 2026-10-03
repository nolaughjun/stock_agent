"""selfcheck 单测: --selfcheck 自检的判定与输出 (2026-09-18 产物自证机制)。"""

from __future__ import annotations

import sys
import tkinter
from pathlib import Path

import pytest

from src.app.usb_vault import config, mac_volume
from src.app.usb_vault.viewer import selfcheck


def _stub_screenshot_probe(
        monkeypatch: pytest.MonkeyPatch,
        result: tuple[str, str] = ("ok", "3 个窗口已排除出截屏/录屏")) -> None:
    """stub v2.4.11 防截屏核验行: 探测窗与 objc 链全假 (Windows 测试机无 Darwin 实体)。"""
    class _Probe:
        def withdraw(self) -> None: ...

        def update(self) -> None: ...

        def destroy(self) -> None: ...

    monkeypatch.setattr(tkinter, "Tk", lambda: _Probe())
    monkeypatch.setattr(
        "src.app.usb_vault.viewer.mac_protection.protect_all_windows",
        lambda chain=None: result)


def test_selfcheck_green_on_darwin_with_volume(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """macOS 直读成功: 全 ✔, 退出码 0, 版本与序列号都在输出里。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_screenshot_probe(monkeypatch)
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
    assert "防截屏" in out  # v2.4.11 核验行在线且绿
    assert "自检通过" in out


def test_selfcheck_red_without_volume(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """数据卷找不到: ✗ + 退出码 1 (产物链断在哪一环, 一眼可见)。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_screenshot_probe(monkeypatch)
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
    _stub_screenshot_probe(monkeypatch)
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


# ---- v2.4.8 自身介质闸门预演 (镜像 _startup_gate 顺序, 介质检查在最先) ----


def test_selfcheck_source_run_reports_gate_disabled(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """源码运行 (无 sys.frozen, 烧录机预览): 闸门不启用, 不影响判定。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert selfcheck.run_selfcheck(Path("E:/")) == 0
    out = capsys.readouterr().out
    assert "介质闸门不启用" in out and "自检通过" in out


def test_selfcheck_frozen_on_removable_stays_green(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """冻构建在可移动介质上: 预演放行, 全 ✔ 退出码 0。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_screenshot_probe(monkeypatch)
    # frozen 下 load_embedded_seed 改按 sys.executable (本机=venv python) 解析,
    # 注入假种子防误报 (介质预演才是本用例的被测点)
    monkeypatch.setattr("src.app.usb_vault.seed.load_embedded_seed",
                        lambda: b"S" * 64)
    monkeypatch.setattr(mac_volume, "is_on_external_media", lambda _p: True)
    monkeypatch.setattr(
        "src.app.usb_vault.viewer.app._mac_data_volume_candidates",
        lambda own: [Path("/Volumes/TEST1")],
    )
    monkeypatch.setattr(mac_volume, "get_volume_serial_mac",
                        lambda root: 0xBCBB1A98)
    assert selfcheck.run_selfcheck(Path("/Volumes/TEST1")) == 0
    out = capsys.readouterr().out
    assert "可移动介质 (闸门放行)" in out and "自检通过" in out


def test_selfcheck_frozen_off_removable_fails(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """冻构建不在可移动介质 (拷到电脑的副本): 预演 ✗ 退出码 1, 文案指明
    启动将谢幕 —— 构建机/收件人侧一条命令即可分辨拷贝件。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_screenshot_probe(monkeypatch)
    monkeypatch.setattr("src.app.usb_vault.seed.load_embedded_seed",
                        lambda: b"S" * 64)
    monkeypatch.setattr(mac_volume, "is_on_external_media", lambda _p: False)
    monkeypatch.setattr(
        "src.app.usb_vault.viewer.app._mac_data_volume_candidates",
        lambda own: [Path("/Volumes/TEST1")],
    )
    assert selfcheck.run_selfcheck(Path("/Volumes/TEST1")) == 1
    out = capsys.readouterr().out
    assert "启动将谢幕" in out and "自检未通过" in out


# ---- v2.4.11 防截屏核验行 (darwin 专属) ----


def test_selfcheck_screenshot_protection_fail_red(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """防护 fail (如回读不生效): ✗ + 退出码 1 —— 构建机出炉即知防护是否在岗。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_screenshot_probe(
        monkeypatch, ("fail", "sharingType 仅 0/1 个窗口生效"))
    assert selfcheck.run_selfcheck(Path("/Volumes/P2")) == 1
    out = capsys.readouterr().out
    assert "防截屏" in out and "仅 0/1" in out


def test_selfcheck_screenshot_protection_no_windows_green(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """隐藏探测窗未映射进 NSApp.windows 不算失败 (露窗后常驻重刷自动纳入)。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_screenshot_probe(
        monkeypatch, ("no_windows", "app 无窗口 (稍后重试)"))
    monkeypatch.setattr(
        "src.app.usb_vault.viewer.app._mac_data_volume_candidates",
        lambda own: [Path("/Volumes/TEST1")],
    )
    monkeypatch.setattr(mac_volume, "get_volume_serial_mac",
                        lambda root: 0xBCBB1A98)
    assert selfcheck.run_selfcheck(Path("/Volumes/TEST1")) == 0
    out = capsys.readouterr().out
    assert "露窗后自动纳入" in out and "自检通过" in out
