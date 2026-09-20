"""mac_protection 单测: 非 darwin 守卫 / objc 缺失降级 / after 重试编排 (全 mock)。"""

from __future__ import annotations

import ctypes.util
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from src.app.usb_vault.viewer import mac_protection, state

# 注: mac_protection.sys/.ctypes 与 state.sys 即全局模块对象, 直接 patch 等价


def test_not_darwin_returns_false() -> None:
    ok, note = mac_protection.set_window_sharing_none()
    assert not ok
    assert "macOS" in note


def test_objc_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes.util, "find_library", lambda _n: None)
    ok, note = mac_protection.set_window_sharing_none()
    assert not ok
    assert "libobjc" in note


class _FakeRoot:
    """记录 after 调度的假 Tk 根。"""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, Callable[[], None]]] = []

    def after(self, ms: int, fn: Callable[[], None]) -> None:
        self.scheduled.append((ms, fn))


def test_retry_scheduled_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = {"n": 0}

    def flaky() -> tuple[bool, str]:
        calls["n"] += 1
        if calls["n"] < 3:
            return False, "mainWindow 未就绪 (稍后重试)"
        return True, "ok"
    monkeypatch.setattr(mac_protection, "set_window_sharing_none", flaky)

    root = _FakeRoot()
    mac_protection.apply_window_sharing_protection(root)
    assert calls["n"] == 1
    assert len(root.scheduled) == 1
    _ms, fn1 = root.scheduled[0]
    fn1()  # 第 1 次重试: 仍失败, 继续排
    assert calls["n"] == 2
    _ms, fn2 = root.scheduled[1]
    fn2()  # 第 2 次重试: 成功, 不再排
    assert calls["n"] == 3
    assert len(root.scheduled) == 2


def test_retry_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = {"n": 0}

    def never() -> tuple[bool, str]:
        calls["n"] += 1
        return False, "mainWindow 未就绪 (稍后重试)"
    monkeypatch.setattr(mac_protection, "set_window_sharing_none", never)

    root = _FakeRoot()
    mac_protection.apply_window_sharing_protection(root, attempts=2)
    for _ in range(2):
        root.scheduled[-1][1]()  # 依次触发排程
    assert calls["n"] == 3  # 初次 + 2 次重试后停止
    assert len(root.scheduled) == 2


def test_no_after_falls_back_single_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = {"n": 0}

    def once() -> tuple[bool, str]:
        calls["n"] += 1
        return False, "x"
    monkeypatch.setattr(mac_protection, "set_window_sharing_none", once)
    mac_protection.apply_window_sharing_protection(object())
    assert calls["n"] == 1


# ------------------------------------------------------------------ 状态目录

def test_state_base_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert state.default_state_base() == Path.home() / "Library" / "Application Support"


def test_state_base_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    assert state.default_state_base() == Path(r"C:\Users\u\AppData\Local")
