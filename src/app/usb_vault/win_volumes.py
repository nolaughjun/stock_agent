"""Windows 卷 GUID 枚举 (v2.4.0 反转双分区布局的数据盘寻址)。

布局: 分区1 (可见, 盘符) = 双阅览器; 分区2 (GPT 无盘符属性) = 加密链。
阅览器以普通权限经 ``\\\\?\\Volume{GUID}\\`` 路径访问分区2 —— 无盘符卷仍被
系统挂载, 文件级访问不需管理员。2026-09-22 探针实证: 非提权进程对无盘符
NTFS 卷 iterdir/读文件成功; EFI 分区的拒绝访问属微软引导分区专项加固
(防 rootkit), exFAT 数据分区不在其列 —— 真机 F: 棒复测列入验证手册。

真假数据盘的仲裁 (GCM 试解) 在 viewer.app._resolve_data_drive; 本模块只
负责"把候选卷根按优先级排出来": 自身卷 → 无盘符卷 → 其余带盘符卷
(mbr 制式靠无盘符隐藏, 收件人 Windows 机可能自动补盘符, 第三序兜住)。
枚举失败的卷 (如 ESP) 抛 OSError 即跳过 —— 见 README 安全边界一节。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path
from typing import Callable

from src.app.usb_vault import config

# 每卷信息: (卷 GUID 根路径, 该卷的全部盘符路径; 无盘符则为空表)
VolumeInfo = tuple[str, list[str]]

# 非 win32 无 windll —— Mac 构建机的 PyInstaller 也会 import 本模块
# (hidden import), 绑定放条件内, 平台挡在 data_volume_candidates 入口
_k32 = getattr(ctypes, "windll", None)
if _k32 is not None:
    _k32 = _k32.kernel32
    # 探针教训 (2026-09-22): FindFirstVolumeW 返回 64 位卷枚举句柄, 默认
    # c_int restype 会截断高位 → 访问违例崩溃。句柄一律走 c_void_p。
    _k32.FindFirstVolumeW.restype = ctypes.c_void_p
    _k32.FindFirstVolumeW.argtypes = [wt.LPWSTR, wt.DWORD]
    _k32.FindNextVolumeW.restype = wt.BOOL
    _k32.FindNextVolumeW.argtypes = [ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
    _k32.FindVolumeClose.argtypes = [ctypes.c_void_p]
    _k32.GetVolumePathNamesForVolumeNameW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD, wt.LPDWORD]

_BUF = 1024  # MSDN: 卷名缓冲区至少 50 字符, 取宽


def _list_volumes() -> list[VolumeInfo]:
    """枚举所有已挂载卷: [(GUID 根路径, [盘符路径...]), ...]。

    枚举本身 (FindFirstVolumeW 家族) 不触碰卷内容, 普通权限即可。
    """
    assert _k32 is not None  # 调用方 (data_volume_candidates) 已挡非 win32
    buf = ctypes.create_unicode_buffer(_BUF)
    handle = _k32.FindFirstVolumeW(buf, _BUF)
    if not handle:  # 句柄为 INVALID_HANDLE_VALUE
        raise OSError("FindFirstVolumeW 失败")
    volumes: list[VolumeInfo] = []
    try:
        while True:
            guid_root = buf.value
            paths = ctypes.create_unicode_buffer(512)
            ret = wt.DWORD()
            _k32.GetVolumePathNamesForVolumeNameW(guid_root, paths, 512, ctypes.byref(ret))
            # 多盘符时缓冲区为 "E:\\\0F:\\\0\0"; 失败(如权限)则当无盘符处理
            letters = [chunk for chunk in paths.value.split("\0") if chunk]
            volumes.append((guid_root, letters))
            if not _k32.FindNextVolumeW(handle, buf, _BUF):
                break
    finally:
        _k32.FindVolumeClose(handle)
    return volumes


def data_volume_candidates(
    own: Path | None = None,
    *,
    list_volumes: Callable[[], list[VolumeInfo]] = _list_volumes,
    keyinfo_name: str = config.KEYINFO_NAME,
) -> list[Path]:
    """数据卷候选序 (win32): 自身卷 → 无盘符卷 (GUID 根) → 其余带盘符卷,
    只收 keyinfo 命中的 (与 macOS 候选同一契约, 真假仲裁交给 GCM 试解)。

    自身卷有载体 = 单分区盘, 退化为 [自身] (旧版行为); 自身卷无载体
    (反转布局: 阅览器在分区1, 加密链在无盘符分区2) 才扫全机卷。个别卷
    拒访 (如 ESP 的引导分区专项加固) 按卷跳过, 不拖垮整个候选序。
    非 win32 平台返回 []。
    """
    if sys.platform != "win32":
        return []
    if own is not None:
        try:
            if (own / keyinfo_name).is_file():
                return [own]  # 单分区盘: 载体就在自身卷, 不必扫全机卷
        except OSError:
            pass  # 自身卷都读不了: 继续扫兄弟卷, 门禁最终给可见报错
    own_letter = _drive_spec(own) if own is not None else None
    candidates: list[Path] = []
    try:
        volumes = list_volumes()
    except OSError:
        return candidates  # 枚举整体失败: 返回空, 门禁按"脱离U盘介质"报错
    no_letter: list[Path] = []
    lettered: list[Path] = []
    for guid_root, letters in volumes:
        root = Path(guid_root)
        try:
            if not (root / keyinfo_name).is_file():
                continue  # 无载体: 与本阅览器无关的卷
        except OSError:
            continue  # 拒访卷 (如 ESP): 跳过, 详见模块 docstring
        if not letters:  # 无盘符卷 (反转布局的正主): GUID 根进入
            no_letter.append(root)
            continue
        if own_letter is not None and any(
            _drive_spec(Path(letter)) == own_letter for letter in letters
        ):
            continue  # 自身卷的 GUID 形态 (无载体才走到这): 不进候选
        lettered.append(Path(letters[0]))  # 带盘符卷以盘符形态进入 (mbr 兜底)
    return no_letter + lettered


def _drive_spec(path: Path) -> str | None:
    """路径的盘符 ("E:") —— 非盘符路径 (UNC/GUID) 返回 None。"""
    drive = path.anchor[:2] if len(path.anchor) >= 2 else ""
    return drive.upper() if len(drive) == 2 and drive[1] == ":" else None
