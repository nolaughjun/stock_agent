"""macOS 平台防护 (设计 D3): 不依赖 pyobjc, 纯 ctypes 调内建 Objective-C 运行时。

能力与边界 (README 同步声明):
- NSWindowSharingNone: 本窗口不出现在 CGWindowList 捕获链 —— 系统截图
  (⌘⇧3/4)、录屏、第三方截屏工具都拍不到窗口内容 (QuickTime 播 DRM
  视频不被截屏的同款机制)
- ⌘ 复制键位由 app.py 按 macOS 序列吞掉 (与 Windows 的 Ctrl 序列对偶)
- 防不住手机翻拍物理屏幕 (与 Windows 侧一致, 属方案边界)

所有调用"尽力而为": 任何一步失败只返回 (False, 原因), 绝不挡阅读。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

NS_WINDOW_SHARING_NONE = 0  # NSWindowSharingNone
_RETRY_ATTEMPTS = 4  # mainWindow 在窗口 shown 前是 nil, 定时重试
_RETRY_DELAY_MS = 400


def _load_objc() -> ctypes.CDLL | None:
    try:
        path = ctypes.util.find_library("objc")
        if not path:
            return None
        return ctypes.cdll.LoadLibrary(path)
    except OSError:
        return None


def set_window_sharing_none() -> tuple[bool, str]:
    """把 NSApplication.mainWindow 设为 NSWindowSharingNone。

    返回 (成功?, 说明)。mainWindow 未就绪返回可重试语义的失败原因。
    """
    if sys.platform != "darwin":
        return False, "仅 macOS 生效"
    lib = _load_objc()
    if lib is None:
        return False, "未找到 libobjc"
    try:
        # objc_msgSend 是变参函数; 只传整型/指针参数时, 按每个调用点
        # 的固定原型直接调地址在 x86_64/arm64 上均安全 (整数参数全走寄存器)。
        proto0 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        proto1 = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        proto_void = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        send0 = proto0(lib.objc_msgSend)
        send1 = proto1(lib.objc_msgSend)
        send_void = proto_void(lib.objc_msgSend)

        get_class = proto0(lib.objc_getClass)
        sel = proto0(lib.sel_registerName)

        ns_app_cls = get_class(None, sel(b"NSApplication"))
        if not ns_app_cls:
            return False, "未找到 NSApplication 类"
        ns_app = send0(ns_app_cls, sel(b"sharedApplication"))
        if not ns_app:
            return False, "NSApplication.sharedApplication 为空"
        window = send0(ns_app, sel(b"mainWindow"))
        if not window:
            return False, "mainWindow 未就绪 (稍后重试)"
        send_void(window, sel(b"setSharingType:"), NS_WINDOW_SHARING_NONE)
        # 回读确认
        if int(send1(window, sel(b"sharingType")) or -1) != NS_WINDOW_SHARING_NONE:
            return False, "sharingType 设置未生效"
        return True, "窗口已排除出截屏/录屏 (NSWindowSharingNone)"
    except Exception as exc:  # noqa: BLE001 防护失败不挡阅读
        return False, f"objc 调用失败: {exc}"


def apply_window_sharing_protection(root: object, *, attempts: int = _RETRY_ATTEMPTS,
                                    delay_ms: int = _RETRY_DELAY_MS) -> None:
    """set_window_sharing_none 的 Tk 包装: 失败经 root.after 重试 (mainWindow 晚就绪)。"""
    after = getattr(root, "after", None)
    if after is None:
        set_window_sharing_none()
        return

    def attempt(remaining: int) -> None:
        ok, _note = set_window_sharing_none()
        if not ok and remaining > 0:
            after(delay_ms, lambda: attempt(remaining - 1))

    attempt(attempts)
