"""Windows 平台防护: DPI 感知、防截屏窗口亲和、PrtScr 抑制。

全部为"尽力而为", 失败不影响查看 (仅记录), 边界见 README。
"""

from __future__ import annotations

import ctypes
import sys

WDA_NONE = 0x0
WDA_MONITOR = 0x1  # 仅显示器可见: PrintWindow/BitBlt 截不到 (Win7+)
WDA_EXCLUDEFROMCAPTURE = 0x11  # 完全排除出捕获 (Win10 2004+)


def set_dpi_awareness() -> None:
    """高 DPI 下文字不发糊; 必须在创建 Tk 根窗口之前调用。"""
    if sys.platform != "win32":
        return
    try:
        # Per-Monitor V2 最好; 失败退 SetProcessDpiAwareness(2)=系统级
        r = ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4))
        if r:
            return
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 老系统无 shcore/旧 user32: 放弃, 不影响功能
        pass


def apply_window_protection(hwnd: int) -> bool:
    """把窗口标记为"排除出截屏"; 老系统降级 WDA_MONITOR, 再失败返回 False。"""
    if sys.platform != "win32" or not hwnd:
        return False
    for value in (WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR):
        try:
            if ctypes.windll.user32.SetWindowDisplayAffinity(
                    ctypes.c_void_p(hwnd), value):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def suppress_printscreen(root: object) -> None:
    """把 PrintScreen 键吞掉 (仅当本程序持有焦点时生效)。"""
    def _swallow(_event: object) -> str:
        return "break"

    try:
        root.bind_all("<Print>", _swallow)  # type: ignore[attr-defined]
        root.bind_all("<Print-Screen>", _swallow)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
