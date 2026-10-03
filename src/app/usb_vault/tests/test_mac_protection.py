"""mac_protection 单测 (v2.4.11): 全窗口防护 / 回读核验 / 常驻重刷 / 失效留痕 (全 mock)。"""

from __future__ import annotations

import ctypes.util
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from src.app.usb_vault.viewer import mac_protection, state


class _FakeChain(mac_protection._ObjcChain):
    """按 selector 名路由的假 objc 链: 不碰真 ctypes, 窗口=整数句柄。

    make_sticky 模拟"setSharingType: 调了但回读仍是 ReadOnly"的窗口 ——
    protect_all_windows 不信设置调用本身, 必须靠它逼出 fail 分支。
    """

    _WINS = 0x57574E53  # windows 数组的假句柄 (真实现里是 NSArray 指针)

    def __init__(self, windows: list[int], *, shared_app: int = 1,
                 ns_class: bool = True, boom: bool = False) -> None:
        self.windows = windows
        self._shared_app = shared_app
        self._ns_class = ns_class
        self._boom = boom
        self._sticky: set[int] = set()
        self._sharing: dict[int, int] = {w: 1 for w in windows}  # 默认 ReadOnly
        self._sel_by_name: dict[str, int] = {}
        self._name_by_sel: dict[int, str] = {}
        self.set_calls: list[int] = []

    def make_sticky(self, *wins: int) -> None:
        self._sticky.update(wins)

    def cls(self, name: str) -> int:
        return 1 if name == "NSApplication" and self._ns_class else 0

    def sel(self, name: str) -> int:
        if name not in self._sel_by_name:
            self._sel_by_name[name] = len(self._sel_by_name) + 1
            self._name_by_sel[self._sel_by_name[name]] = name
        return self._sel_by_name[name]

    def _name(self, s: int) -> str:
        return self._name_by_sel.get(s, "?")

    def send_id(self, obj: int, s: int) -> int:
        if self._boom:
            raise RuntimeError("objc_msgSend 崩了")
        name = self._name(s)
        if name == "sharedApplication":
            return self._shared_app
        if name == "windows":
            return self._WINS
        return 0

    def send_id_u(self, obj: int, s: int, n: int) -> int:
        if self._boom:
            raise RuntimeError("objc_msgSend 崩了")
        if self._name(s) == "objectAtIndex:" and 0 <= n < len(self.windows):
            return self.windows[n]
        return 0

    def send_uint(self, obj: int, s: int) -> int:
        if self._boom:
            raise RuntimeError("objc_msgSend 崩了")
        name = self._name(s)
        if name == "count":
            return len(self.windows)
        if name == "sharingType":
            return self._sharing.get(obj, 1)
        return 0

    def send_void_u(self, obj: int, s: int, n: int) -> None:
        self.set_calls.append(obj)
        if obj not in self._sticky:
            self._sharing[obj] = n


# ------------------------------------------------------------------ protect_all_windows

def test_not_darwin_refuses() -> None:
    status, note = mac_protection.protect_all_windows()
    assert (status, note) == ("not_darwin", "仅 macOS 生效")


def test_no_objc_leaves_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes.util, "find_library", lambda _n: None)
    status, note = mac_protection.protect_all_windows()
    assert status == "no_objc"
    assert "libobjc" in note


def test_all_windows_protected_with_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐窗设置+回读: 3 个窗口 (含 Toplevel 各自的 NSWindow) 全部生效才 ok。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    chain = _FakeChain([11, 22, 33])
    status, note = mac_protection.protect_all_windows(chain)
    assert status == "ok"
    assert "3 个窗口" in note
    assert chain.set_calls == [11, 22, 33]
    for w in (11, 22, 33):
        assert chain.send_uint(w, chain.sel("sharingType")) == 0  # 回读源即已置 None


def test_partial_readback_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """设置调了但回读没变 (v2.4.11 前"静默裸奔"的病根): 必须 fail 并报比例。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    chain = _FakeChain([11, 22])
    chain.make_sticky(22)
    status, note = mac_protection.protect_all_windows(chain)
    assert status == "fail"
    assert "仅 1/2" in note


def test_no_windows_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    status, note = mac_protection.protect_all_windows(_FakeChain([]))
    assert status == "no_windows"
    assert "稍后重试" in note


def test_shared_application_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    status, note = mac_protection.protect_all_windows(_FakeChain([11], shared_app=0))
    assert status == "fail"
    assert "sharedApplication 为空" in note


def test_nsapplication_class_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    status, note = mac_protection.protect_all_windows(_FakeChain([11], ns_class=False))
    assert status == "fail"
    assert "未找到 NSApplication 类" in note


def test_msgsend_exception_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    status, note = mac_protection.protect_all_windows(_FakeChain([11], boom=True))
    assert status == "fail"
    assert "objc 调用失败" in note


# ------------------------------------------------------------------ start_window_protection

class _FakeRoot:
    """记录 after 调度的假 Tk 根; after_raises 模拟窗口已销毁。"""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, Callable[[], None]]] = []
        self.after_raises: BaseException | None = None

    def after(self, ms: int, fn: Callable[[], None]) -> None:
        if self.after_raises is not None:
            raise self.after_raises
        self.scheduled.append((ms, fn))


def _scripted(monkeypatch: pytest.MonkeyPatch,
              results: list[tuple[str, str]]) -> dict[str, int]:
    """把 protect_all_windows 替换成按剧本逐条返回的假实现。"""
    calls = {"n": 0}

    def fake(_chain: object | None = None) -> tuple[str, str]:
        calls["n"] += 1
        return results[min(calls["n"], len(results)) - 1]

    monkeypatch.setattr(mac_protection, "protect_all_windows", fake)
    return calls


def test_loop_keeps_refreshing_while_ok(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """常驻循环而非"成功即停": ok 之后仍按周期重刷 (新开窗口自动纳入)。"""
    calls = _scripted(monkeypatch, [("ok", "a"), ("ok", "b")])
    root = _FakeRoot()
    mac_protection.start_window_protection(root)
    assert calls["n"] == 1 and len(root.scheduled) == 1
    assert root.scheduled[0][0] == mac_protection._LOOP_MS  # 默认 2s
    root.scheduled[-1][1]()  # 重刷后继续排程, 循环不断
    assert calls["n"] == 2 and len(root.scheduled) == 2


def test_degradation_prints_once_per_transition(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """健康→失效打一行 stderr, 持续失效不刷屏, 恢复后再失效才再打 (失败必留痕)。"""
    calls = _scripted(monkeypatch, [("ok", "a"), ("fail", "坏1"),
                                    ("fail", "坏2"), ("ok", "b"), ("fail", "坏3")])
    root = _FakeRoot()
    mac_protection.start_window_protection(root)  # tick1 ok: 无事
    root.scheduled[-1][1]()  # tick2 fail: 恶化 → 打第 1 行
    root.scheduled[-1][1]()  # tick3 fail: 仍坏 → 不再打
    root.scheduled[-1][1]()  # tick4 ok: 恢复
    root.scheduled[-1][1]()  # tick5 fail: 再恶化 → 打第 2 行
    assert calls["n"] == 5
    err = capsys.readouterr().err
    assert err.count("防截屏失效") == 2
    assert "坏1" in err and "坏3" in err and "坏2" not in err


def test_first_tick_failure_prints(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """启动即失效也要留痕 —— v2.3.3 静默失效事故的直接反面。"""
    _scripted(monkeypatch, [("fail", "objc 调用失败: x")])
    mac_protection.start_window_protection(_FakeRoot())
    err = capsys.readouterr().err
    assert "防截屏失效 (fail)" in err and "objc 调用失败" in err


def test_root_destroyed_stops_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """root.after 抛错 (窗口销毁): 吞掉即自然停摆, 不再排程。"""
    calls = _scripted(monkeypatch, [("ok", "a")])
    root = _FakeRoot()
    root.after_raises = RuntimeError("can't invoke after command: destroyed")
    mac_protection.start_window_protection(root)
    assert calls["n"] == 1 and root.scheduled == []


# ------------------------------------------------------------------ 状态目录

def test_state_base_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert state.default_state_base() == Path.home() / "Library" / "Application Support"


def test_state_base_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    assert state.default_state_base() == Path(r"C:\Users\u\AppData\Local")
