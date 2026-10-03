"""win_protection 防截屏回归 (2026-09-20 v2.3.4 收件人真机实测曝光)。

v2.3.3 及之前 SetWindowDisplayAffinity 一直设在 Tk winfo_id() 返回的
内层子 HWND 上 —— 系统静默拒绝, 防护从未生效, 微信截图/FSCapture
录屏均能拍到内容。本用例用真实 Tk 窗口锁住两件事: ①亲和性必须设到
GetAncestor(GA_ROOT) 的顶层窗口上; ②系统回读确认所设值非零。
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from src.app.usb_vault.viewer import win_protection

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows 窗口亲和性防护")


def test_apply_window_protection_sets_affinity_on_root_hwnd() -> None:
    """防护必须落在顶层 HWND 并可被系统回读 (黑块承诺的机制锚点)。"""
    import tkinter as tk

    root = tk.Tk()
    root.title("affinity-test")
    root.geometry("120x80+40+40")
    root.update_idletasks()
    root.update()
    try:
        assert win_protection.apply_window_protection(root.winfo_id()), (
            "对真实窗口设置 display affinity 失败 —— 防截屏未生效")
        user32 = ctypes.windll.user32
        top = user32.GetAncestor(root.winfo_id(), win_protection.GA_ROOT)
        assert top, "GetAncestor(GA_ROOT) 未取到顶层窗口"
        aff = ctypes.c_uint(win_protection.WDA_NONE)
        ok = user32.GetWindowDisplayAffinity(ctypes.c_void_p(top),
                                             ctypes.byref(aff))
        assert ok, "GetWindowDisplayAffinity 回读失败"
        assert aff.value != win_protection.WDA_NONE, (
            "顶层窗口亲和性仍为 WDA_NONE —— 设置没落在顶层 HWND 上 "
            "(v2.3.3 缺陷即此形态)")
    finally:
        root.destroy()
