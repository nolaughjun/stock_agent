"""macOS 平台防护 (设计 D3): 不依赖 pyobjc, 纯 ctypes 调内建 Objective-C 运行时。

能力与边界 (README 同步声明):
- NSWindowSharingNone: 窗口服务器拒绝把窗口内容合成进 CGWindowList 捕获
  链 —— 系统截图 (⌘⇧3/4)、screencapture、以此管线实现的第三方截屏工具
  (微信截图等) 拍不到窗口内容 (1Password/银行类应用同款机制)
- ⌘ 复制键位由 app.py 按 macOS 序列吞掉 (与 Windows 的 Ctrl 序列对偶)
- 防不住手机翻拍物理屏幕 (与 Windows 侧一致, 属方案边界)

v2.4.11 (2026-10-01 收件 Mac 微信截屏实锤) 前的实现只把 mainWindow 设
一次 + 4×400ms 重试预算 —— U盘冷启动慢于预算即静默裸奔, 失败无日志、
自检无核验 (v2.3.3 Windows 防截屏"静默失效"事故的同款病, 换平台复发)。
现在: protect_all_windows 对 NSApp.windows **逐个**设置并回读核验;
start_window_protection 常驻每 2s 重刷 (后开的对话框窗口自动纳入),
状态恶化打 stderr; selfcheck 有"防截屏"行在冻结产物里实证核验。

所有调用"尽力而为": 任何一步失败只返回失败状态与原因, 绝不挡阅读。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

NS_WINDOW_SHARING_NONE = 0  # NSWindowSharingNone (默认 ReadOnly=1)
_LOOP_MS = 2000  # 常驻重刷周期: 新建 Toplevel 的 NSWindow 自动纳入


class _ObjcChain:
    """libobjc 最小封装: 只包本模块用到的原型, 独立成类纯粹为了测试可注入。

    objc_msgSend 是变参函数; 只传指针/整型参数时按调用点固定原型取地址
    调用在 x86_64/arm64 上均安全 (整数参数全走寄存器)。
    """

    def __init__(self, lib: ctypes.CDLL) -> None:
        p_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        p_id_u = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
        p_u = ctypes.CFUNCTYPE(
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)
        p_v_u = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
        p_name = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)
        self._get_class = p_name(lib.objc_getClass)
        self._sel = p_name(lib.sel_registerName)
        self._send_id = p_id(lib.objc_msgSend)
        self._send_id_u = p_id_u(lib.objc_msgSend)
        self._send_u = p_u(lib.objc_msgSend)
        self._send_v_u = p_v_u(lib.objc_msgSend)

    def cls(self, name: str) -> int:
        return self._get_class(name.encode()) or 0

    def sel(self, name: str) -> int:
        return self._sel(name.encode()) or 0

    def send_id(self, obj: int, s: int) -> int:
        return self._send_id(obj, s) or 0

    def send_id_u(self, obj: int, s: int, n: int) -> int:
        return self._send_id_u(obj, s, n) or 0

    def send_uint(self, obj: int, s: int) -> int:
        return int(self._send_u(obj, s) or 0)

    def send_void_u(self, obj: int, s: int, n: int) -> None:
        self._send_v_u(obj, s, n)


def _load_chain() -> _ObjcChain | None:
    try:
        path = ctypes.util.find_library("objc")
        if not path:
            return None
        return _ObjcChain(ctypes.cdll.LoadLibrary(path))
    except OSError:
        return None


def protect_all_windows(chain: _ObjcChain | None = None) -> tuple[str, str]:
    """把 NSApp.windows 里**每个** NSWindow/NSPanel 置 NSWindowSharingNone。

    返回 (状态, 说明), 状态 ∈ ok / no_windows (窗口未就绪, 可重试) /
    no_objc / fail / not_darwin。逐窗设置后**回读核验** (不信设置调用
    本身 —— ntfs_guard 同款原则), 全部生效才是 ok。

    只盯 mainWindow 是 v2.4.11 前的坑: 解锁码框等 Toplevel 各有自己的
    NSWindow, mainWindow 一个罩不全; 且 Tk 冷启动时 mainWindow 可能晚于
    重试预算就绪。
    """
    if sys.platform != "darwin":
        return "not_darwin", "仅 macOS 生效"
    if chain is None:
        chain = _load_chain()
    if chain is None:
        return "no_objc", "未找到 libobjc"
    try:
        app_cls = chain.cls("NSApplication")
        if not app_cls:
            return "fail", "未找到 NSApplication 类"
        app = chain.send_id(app_cls, chain.sel("sharedApplication"))
        if not app:
            return "fail", "NSApplication.sharedApplication 为空"
        wins = chain.send_id(app, chain.sel("windows"))
        if not wins:
            return "fail", "NSApplication.windows 取不到"
        total = chain.send_uint(wins, chain.sel("count"))
        if total <= 0:
            return "no_windows", "app 无窗口 (稍后重试)"
        protected = 0
        for i in range(total):
            win = chain.send_id_u(wins, chain.sel("objectAtIndex:"), i)
            if not win:
                continue
            chain.send_void_u(
                win, chain.sel("setSharingType:"), NS_WINDOW_SHARING_NONE)
            if chain.send_uint(win, chain.sel("sharingType")) == NS_WINDOW_SHARING_NONE:
                protected += 1
        if protected == total:
            return "ok", f"{total} 个窗口已排除出截屏/录屏 (NSWindowSharingNone)"
        return "fail", f"sharingType 仅 {protected}/{total} 个窗口生效"
    except Exception as exc:  # noqa: BLE001 防护失败不挡阅读
        return "fail", f"objc 调用失败: {exc}"


def start_window_protection(root: object, *, loop_ms: int = _LOOP_MS) -> None:
    """常驻防护: 立即设一轮, 之后每 loop_ms 重刷 (新建对话框自动纳入)。

    状态恶化 (健康→fail/no_objc) 打一行 stderr, 恢复后再恶化才再打 ——
    对齐 win_protection "失败必留痕" 的教训 (v2.3.3 静默失效不能重演)。
    root 销毁后 after 回调抛错, 吞掉即自然停摆。
    """
    state: dict[str, object] = {"healthy": True}

    def tick() -> None:
        status, detail = protect_all_windows()
        healthy = status == "ok"
        if not healthy and state["healthy"]:
            if sys.stderr is not None:
                print(f"[usb_vault] 防截屏失效 ({status}): {detail}", file=sys.stderr)
        state["healthy"] = healthy
        try:
            root.after(loop_ms, tick)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 窗口已销毁: 循环自然终止
            return

    tick()
