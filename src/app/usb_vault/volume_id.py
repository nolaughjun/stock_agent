"""卷序列号与驱动器信息 (平台分派; 不引入 psutil)。

- Windows: GetVolumeInformationW (纯 ctypes); 对 FAT/exFAT 返回的即引导扇区在盘值
- macOS:   mac_volume 读分区设备节点引导扇区后解析 (与 Windows 端同值, 设计 D2)

卷序列号参与密钥派生: 同一份数据换到别的卷 => 派生密钥不同 => 解密失败。
注意: 序列号可被 VolumeID 类工具伪造, 这是 README 声明的安全边界之一。
"""

from __future__ import annotations

import ctypes
import struct
import sys
from pathlib import Path

_DRIVE_TYPES = {
    0: "unknown",
    1: "no_root",
    2: "removable",
    3: "fixed",
    4: "remote",
    5: "cdrom",
    6: "ramdisk",
}


class VolumeIdError(Exception):
    """卷信息读取失败 (盘不存在 / 路径不是卷根 / API 错误 / 权限不足)。"""


def _volume_root(root: str | Path) -> str:
    """把任意盘符表达归一为 GetVolumeInformationW 要求的 "X:" + 尾部反斜杠 形式。"""
    p = Path(root)
    anchor = p.anchor
    if not anchor:
        raise VolumeIdError(f"不是有效的卷根路径: {root}")
    bs = chr(92)  # 单个反斜杠, 避免转义歧义
    if p.drive and not anchor.endswith((bs, "/")):
        return p.drive + bs
    return anchor


def get_volume_serial(root: str | Path) -> int:
    """返回卷序列号 (uint32), 按平台分派。失败抛 VolumeIdError。"""
    if sys.platform == "darwin":
        # 延迟导入避免与 mac_volume 的循环依赖
        from src.app.usb_vault.mac_volume import get_volume_serial_mac
        return get_volume_serial_mac(root)
    if sys.platform != "win32":
        raise VolumeIdError(f"不支持的平台: {sys.platform}")
    root_str = _volume_root(root)
    volume_name = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong(0)
    max_comp = ctypes.c_ulong(0)
    flags = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root_str),
        volume_name,
        ctypes.sizeof(volume_name),
        ctypes.byref(serial),
        ctypes.byref(max_comp),
        ctypes.byref(flags),
        fs_name,
        ctypes.sizeof(fs_name),
    )
    if not ok:
        err = ctypes.GetLastError()
        raise VolumeIdError(f"读取卷信息失败 (GetVolumeInformationW error={err}): {root_str}")
    return int(serial.value)


def get_volume_serial_candidates(root: str | Path) -> list[int]:
    """返回卷序列号候选 (按置信度排序); 精确可读时恰好 1 个。

    macOS /dev 直读被拒 (收件人非管理员双击 .app 的常态) 时降级
    diskutil VolumeUUID 两候选; Windows 恒 [get_volume_serial] (API 值唯一)。
    仅供查看端配合清单 GCM 认证筛选用 —— 烧录端必须用 get_volume_serial
    (密钥盐须唯一确定值, 猜错 = 对端永远解不开)。
    """
    if sys.platform == "darwin":
        # 延迟导入避免与 mac_volume 的循环依赖
        from src.app.usb_vault.mac_volume import get_volume_serial_candidates_mac
        return get_volume_serial_candidates_mac(root)
    return [get_volume_serial(root)]


def get_volume_fs(root: str | Path) -> str:
    """返回文件系统名, 如 "NTFS" / "FAT32"; 用于 FAT32 4GiB 上限检查。

    仅 Windows (macOS 端烧录前置信息走引导扇区解析, 无需此函数)。
    """
    if sys.platform != "win32":
        raise VolumeIdError("get_volume_fs 仅支持 Windows")
    root_str = _volume_root(root)
    volume_name = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong(0)
    max_comp = ctypes.c_ulong(0)
    flags = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root_str),
        volume_name,
        ctypes.sizeof(volume_name),
        ctypes.byref(serial),
        ctypes.byref(max_comp),
        ctypes.byref(flags),
        fs_name,
        ctypes.sizeof(fs_name),
    )
    if not ok:
        err = ctypes.GetLastError()
        raise VolumeIdError(f"读取文件系统信息失败 (error={err}): {root_str}")
    return fs_name.value


def get_drive_type(root: str | Path) -> str:
    """返回驱动器类型字符串: removable / fixed / cdrom / ... (仅 Windows)。"""
    if sys.platform != "win32":
        raise VolumeIdError("get_drive_type 仅支持 Windows")
    root_str = _volume_root(root)
    code = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root_str))
    return _DRIVE_TYPES.get(code, f"other({code})")


def set_volume_label(root: str | Path, label: str) -> bool:
    """设置卷标 (可选美化); 失败仅返回 False, 不影响烧录。非 Windows 返回 False。"""
    if sys.platform != "win32":
        return False
    root_str = _volume_root(root)
    return bool(ctypes.windll.kernel32.SetVolumeLabelW(
        ctypes.c_wchar_p(root_str), ctypes.c_wchar_p(label)))


def set_hidden(path: str | Path) -> bool:
    """叠加 +H +S 属性 (Windows): 资源管理器默认视图与"显示隐藏的文件"
    均不列出 (后者还需再关掉"隐藏受保护的操作系统文件"才可见, 双重门槛);
    exFAT 的 DOS hidden 位 macOS Finder 同样映射为不可见。
    SetFileAttributesW 是整体替换, 故先读现有属性再叠加 —— 只读等既有位
    原样保留 (apply_readonly=False 的场景不会被顺手锁死)。
    失败仅返回 False 不影响烧录 —— 加密与盘绑定才是安全边界,
    隐藏只是观感层的第一道门。非 Windows 返回 False。
    """
    if sys.platform != "win32":
        return False
    hidden, system, invalid = 0x2, 0x4, 0xFFFFFFFF
    kernel32 = ctypes.windll.kernel32
    pw = ctypes.c_wchar_p(str(path))
    cur = kernel32.GetFileAttributesW(pw)
    mask = (cur if 0 <= cur < invalid else 0) | hidden | system
    return bool(kernel32.SetFileAttributesW(pw, mask))


def serial_to_be(serial: int) -> bytes:
    """卷序列号规范化为 4 字节大端 (跨端一致参与 scrypt 盐)。"""
    if not 0 <= serial <= 0xFFFFFFFF:
        raise VolumeIdError(f"卷序列号超出 uint32 范围: {serial}")
    return struct.pack(">I", serial)


def is_volume_root(path: str | Path) -> bool:
    """判断路径是否为卷根: Windows 如 E:/; macOS 如 /Volumes/卷名。"""
    p = Path(path)
    if sys.platform == "darwin":
        return p.parent == Path("/Volumes")
    return bool(p.anchor) and p.parent == p
