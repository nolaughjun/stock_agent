"""双入口盘的书名引导壳入口 (v2.4.5; v2.4.6 三态)。

收件人双击的书名 exe (VIEWER_INNER_EXE_NAME_DUAL) 是本模块的 onefile
构建产物, 而不是真阅览器: onedir 主 exe 被单独拷离目录后死在 PyInstaller
C 装载器层 ("Failed to load Python DLL ... 找不到指定模块"), Python 尚未
启动, v2.4.4 的应用层谢幕门禁没有机会跑 (2026-09-23 用户复测曝光)。
壳自包含 (onefile) 才能把中文文案送到用户眼前:

- 目录完整 (旁边有真主程序 VIEWER_EXE_NAME + _internal): 无缝转交真主
  程序, 命令行参数原样透传 (--selfcheck 等自检路径不受影响);
- 壳被单独拷走 (_internal 也不在): 弹 DETACHED_MESSAGE 谢幕框;
- _internal 在而真主程序缺 (v2.4.6 真棒事故: 真主程序顶着一代旧名被当
  残留清掉 / 或被杀毒隔离): 这是坏盘不是拷离, 弹 BROKEN_INSTALL_MESSAGE,
  与谢幕文案可分辨, 不误导排障方向。

依赖刻意压到 ctypes/subprocess/sys/pathlib —— 不进 tkinter/PIL/PyMuPDF:
产物 ~10MB、解包亚秒级, 也不给杀毒引擎添一张新的大签名面。文案与
主程序共用 config (单一事实源)。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.app.usb_vault.config import (
    BROKEN_INSTALL_MESSAGE,
    DETACHED_MESSAGE,
    VIEWER_EXE_NAME,
)

_BOX_TITLE = "图书阅览器"


def resolve_target(here: Path, viewer_exe_name: str = VIEWER_EXE_NAME) -> Path | None:
    """目录完整 → 真主程序 exe 路径; 残缺 (被单独拷走) → None。

    判据只有两条: 同目录下的主程序文件 + 其 _internal 依赖树。壳不认
    目录名 (VIEWER_DIR_NAME/双入口目录名都不看) —— 收件人把整个目录改名
    搬走不影晌转交; 缺任何一样都按"脱离介质"谢幕。
    """
    target = here / viewer_exe_name
    internal = here / "_internal"
    try:
        if target.is_file() and internal.is_dir():
            return target
    except OSError:  # 拷贝中断/网络盘等: 一律按残缺处理, 不猜
        pass
    return None


def show_message_box(text: str) -> None:
    """提示框: 原生 MessageBoxW (无 Tk, 拷到任何 Windows 都能弹)。"""
    import ctypes

    MB_OK = 0x0
    MB_ICONINFORMATION = 0x40
    ctypes.windll.user32.MessageBoxW(0, text, _BOX_TITLE, MB_OK | MB_ICONINFORMATION)


def main(argv: list[str] | None = None) -> int:
    """引导壳主流程: 转交真主程序 (0) 或提示后退出 (1)。"""
    args = list(sys.argv[1:] if argv is None else argv)
    here = Path(sys.executable).parent  # onefile 冻结后 = 壳自身所在目录
    target = resolve_target(here)
    if target is not None:
        # GUI 程序: 启动即退场, 不等待子进程 (壳先退出不影响真主程序存活)
        subprocess.Popen([str(target), *args], cwd=str(here))
        return 0
    # _internal 在而真主程序缺 = 盘上文件不完整 (被删/被杀毒隔离), 不是拷离
    message = BROKEN_INSTALL_MESSAGE if (here / "_internal").is_dir() else DETACHED_MESSAGE
    if sys.platform == "win32":
        show_message_box(message)
    else:  # 理论路径: 壳只在 Windows 构建; 保底也把文案送到 stdout
        print(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
