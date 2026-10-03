"""NTFS 防误删守卫 (2026-09-28 用户评审通过; 探针实证先行, 不做未测断言)。

交付棒转 NTFS 后, 防误删不再靠 +R 只读位 (2026-09-20 探针实证
Explorer/SHFileOperationW 会剥只读位再删), 改靠 NTFS deny ACE —— 内核访问
检查层强制的拒绝, Explorer 剥不掉。2026-09-28 本机 NTFS 探针矩阵 (六路
删除 × 三种变体) 定案:

  ship 配方 (分区根 deny (DC) + 交付树 deny (OI)(CI)(DE,DC) + 散文件
  deny (DE)): os.remove / cmd del / cmd del /f / PowerShell Remove-Item
  -Force / Explorer 右键删进回收站 / Shift+Del 永久删, 全部 BLOCKED,
  含整树永久删; 读取/复制不受影响; 后落盘的文件被继承自动罩住。
  half (只拒文件, 父目录放行): 六路全穿 —— 父目录通道兜底删掉。
  文件干净 + 仅拒父目录 (DC): 文件通道照删。
  => Windows 删除的充要条件 = 文件 DELETE **或** 父目录 FILE_DELETE_CHILD,
     得一即可, 故配方必须两级都拒。

边界 (防误删不防蓄意, README/手册如实声明): 所有者/管理员 icacls
/remove:d /t 整树摘除后可删可改; 格式化/diskpart clean 是卷级操作, ACL
无从干涉 (复烧的 clean 重建因此不受影响)。

实证坑 (2026-09-28 微探针): icacls 单文件 /remove:d 对**继承来**的 deny
返回成功但实际没摘 (rc=0 撒谎) —— 交付树清障必须整树 /t
(strip_delete_denial; 分区根例外走非递归, 详见其 docstring), 校验必须回读
ACL (deny_rights 走 GetNamedSecurityInfoW + SID 字符串比对, 免本地化、
不信任何退出码)。

副作用 (手册声明): 改名/移动同被拒 (与删除同需 DELETE); deny 只含 DE/DC
两个 specific 位, 不碰读/写/执行 (2026-09-15 --acl 教训: generic 位连读
带执行一起拒)。

非 win32 平台: fs_name 恒 "" (调用方自然跳过施加), 本模块可被安全导入
(习语同 win_volumes.py, 绑定放条件内)。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

from src.app.usb_vault import config, volume_id

_EVERYONE_SID = "S-1-1-0"  # deny_rights 回读比对形态 (icacls 实参形态见 config.DEL_DENY_SID)

# 访问掩码位 (specific, 非 generic)
_RIGHT_DELETE = 0x00010000
_RIGHT_FILE_DELETE_CHILD = 0x00000040
# ACE 类型/标志
_ACE_TYPE_DENIED = 1  # ACCESS_DENIED_ACE_TYPE
_FLAG_OBJECT_INHERIT = 0x1
_FLAG_CONTAINER_INHERIT = 0x2
_FLAGS_TREE_INHERIT = _FLAG_OBJECT_INHERIT | _FLAG_CONTAINER_INHERIT

# GetNamedSecurityInfoW 参数
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 4

_EXT_PREFIX = chr(92) * 2 + "?" + chr(92)  # \\?\ 长路径前缀


class NtfsGuardError(Exception):
    """防误删 ACL 施加/清障/回读失败。"""


_advapi = getattr(ctypes, "windll", None)
if _advapi is not None:  # 非 win32 无 windll, 绑定放条件内
    _advapi = _advapi.advapi32
    _advapi.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi.GetNamedSecurityInfoW.restype = ctypes.c_uint
    _advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    _advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    _kernel32 = ctypes.windll.kernel32
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _ext(p: Path) -> str:
    """icacls/ACL API 的路径形态: 超 MAX_PATH 加扩展长度前缀 (同 burner.extended_path)。"""
    s = str(p)
    if len(s) >= 248 and not s.startswith(_EXT_PREFIX):
        return _EXT_PREFIX + s
    return s


def fs_name(root: Path) -> str:
    """卷文件系统名 (如 "NTFS"/"exFAT"); 非 Windows / 读不出 = "" (绝不抛)。"""
    if sys.platform != "win32":
        return ""
    try:
        return volume_id.get_volume_fs(root)
    except Exception:  # noqa: BLE001 —— 前置信息类, 任何失败都按"读不出"处理
        return ""


def is_ntfs(root: Path) -> bool:
    return fs_name(root).upper() == "NTFS"


# ---------------------------------------------------------------------------
# ACL 回读 (校验/幂等判断的唯一事实源, 不信 icacls 退出码)
# ---------------------------------------------------------------------------


def deny_rights(path: Path) -> list[tuple[int, int]]:
    """回读 path 的 DACL 中全员 (S-1-1-0) deny ACE, 返回 [(AceFlags, AccessMask)]。

    GetNamedSecurityInfoW + ConvertSidToStringSidW 全程 SID 比对, 免本地化
    组名; 无 DACL (FAT 家族) / 空 DACL 返回 []。
    """
    if _advapi is None:
        raise NtfsGuardError("ACL 回读仅 Windows 可用")
    sd = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    rc = _advapi.GetNamedSecurityInfoW(
        _ext(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
        None, None, ctypes.byref(dacl), None, ctypes.byref(sd),
    )
    try:
        if rc != 0:
            raise NtfsGuardError(f"读取 ACL 失败 (GetNamedSecurityInfoW rc={rc}): {path}")
        if not dacl.value:
            return []  # 无 DACL = 全允许 (FAT 家族挂载形态)
        out: list[tuple[int, int]] = []
        header = ctypes.string_at(dacl.value, 8)  # ACL 头: rev, sz1, size, count, sz2
        ace_count = int.from_bytes(header[4:6], "little")
        off = dacl.value + 8
        for _ in range(ace_count):
            ace_head = ctypes.string_at(off, 4)  # ACE_HEADER: type, flags, size
            ace_size = int.from_bytes(ace_head[2:4], "little")
            if ace_size < 8:  # 结构损坏, 不再前进 (防死循环)
                break
            if ace_head[0] == _ACE_TYPE_DENIED:
                mask = int.from_bytes(ctypes.string_at(off + 4, 4), "little")
                buf = ctypes.c_wchar_p()
                if _advapi.ConvertSidToStringSidW(ctypes.c_void_p(off + 8), ctypes.byref(buf)):
                    try:
                        if buf.value == _EVERYONE_SID:
                            out.append((ace_head[1], mask))
                    finally:
                        _kernel32.LocalFree(ctypes.cast(buf, ctypes.c_void_p))
            off += ace_size
        return out
    finally:
        if sd.value:
            _kernel32.LocalFree(ctypes.c_void_p(sd.value))


def has_delete_deny(path: Path) -> bool:
    """文件自身 DACL 是否拒 DELETE (散文件防线的在位判据)。"""
    return any(mask & _RIGHT_DELETE for _flags, mask in deny_rights(path))


def has_child_deny(directory: Path) -> bool:
    """目录自身 DACL 是否拒 FILE_DELETE_CHILD (分区根防线的在位判据)。"""
    return any(mask & _RIGHT_FILE_DELETE_CHILD for _flags, mask in deny_rights(directory))


def tree_deny_present(tree: Path) -> bool:
    """交付树根是否带 (OI)(CI)(DE,DC) 继承拒删 ACE。"""
    return any(
        (mask & (_RIGHT_DELETE | _RIGHT_FILE_DELETE_CHILD))
        == (_RIGHT_DELETE | _RIGHT_FILE_DELETE_CHILD)
        and (flags & _FLAGS_TREE_INHERIT) == _FLAGS_TREE_INHERIT
        for flags, mask in deny_rights(tree)
    )


# ---------------------------------------------------------------------------
# 施加 / 清障 (icacls; 调用方先过 is_ntfs 门)
# ---------------------------------------------------------------------------


def _icacls(path: Path, *args: str, timeout_s: float = 180.0) -> None:
    """执行 icacls; 失败抛 NtfsGuardError (文案带输出尾部)。"""
    try:
        r = subprocess.run(
            ["icacls", _ext(path), *args], capture_output=True, timeout=timeout_s
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NtfsGuardError(f"icacls 执行失败 ({exc}): {path}") from exc
    if r.returncode != 0:
        tail = ((r.stdout or b"")[-300:] + b" | " + (r.stderr or b"")[-200:])
        raise NtfsGuardError(
            f"icacls 失败 (rc={r.returncode}): {tail.decode('utf-8', 'replace').strip()}"
        )


def deny_delete_tree(tree: Path) -> bool:
    """交付树根施加 (OI)(CI)(DE,DC) 拒删, 继承罩住树内全部文件/子目录
    (含施加之后才落盘的文件)。已在位 = 跳过 (幂等), 返回是否实际施加。"""
    if tree_deny_present(tree):
        return False
    _icacls(tree, "/deny", config.DEL_DENY_TREE_SPEC)
    return True


def deny_delete_children(root: Path) -> bool:
    """分区根施加 (DC) 拒删: 封"删根下任何子项/整棵树"的父目录通道。
    已在位 = 跳过, 返回是否实际施加。"""
    if has_child_deny(root):
        return False
    _icacls(root, "/deny", config.DEL_DENY_CHILDREN_SPEC)
    return True


def deny_delete_file(path: Path) -> bool:
    """分区根下的散文件 (keyinfo/manifest/散 exe) 施加 (DE) 拒删。
    已在位 = 跳过, 返回是否实际施加。"""
    if has_delete_deny(path):
        return False
    _icacls(path, "/deny", config.DEL_DENY_FILE_SPEC)
    return True


def strip_delete_denial(root: Path, *, recurse: bool = True) -> None:
    """清障: 摘掉本工具施加的 deny ACE (refresh/重烧前必走)。

    2026-09-28 微探针实证: 单文件 /remove:d 对继承来的 deny 返回成功但
    实际没摘 —— 目录必须整树 /t (摘根上可继承 ACE 会连带收回子叶的继承
    副本), 文件单独摘 (其 deny 是显式施加的, 可直接摘)。

    recurse=False 仅供分区根: 配方在分区根上只有非继承 (DC) 一条, 无须
    递归; 而 /t 从分区根出发会踩进 System Volume Information ($RECYCLE.BIN
    同类), 该目录管理员也没有写 DACL 权 → icacls rc=5 (2026-09-29 真机
    事故, 全盘重建即栽此)。交付树/散文件的 deny 由调用方按名单逐项摘,
    不靠根递归兜。
    """
    if root.is_dir() and recurse:
        _icacls(root, "/remove:d", config.DEL_DENY_SID, "/t")
    else:
        _icacls(root, "/remove:d", config.DEL_DENY_SID)


# ---------------------------------------------------------------------------
# 校验 (verify 子命令: 回读配方是否在位)
# ---------------------------------------------------------------------------


def check_delete_denial(
    root: Path,
    *,
    dir_names: tuple[str, ...] = (),
    file_names: tuple[str, ...] = (),
) -> list[str]:
    """回读 root 下的防误删配方是否齐全, 返回问题清单 (空 = 齐全)。

    调用方 (main.cmd_verify) 先判 NTFS; 非 NTFS 卷不进本函数。只检查
    "在场的交付物" (旧名残留不在场即不查), 与烧录侧施加名单同源。
    """
    problems: list[str] = []
    if not has_child_deny(root):
        problems.append(f"{root}: 根缺 (DC) 拒删 ACE (整树删除通道未封)")
    for name in dir_names:
        p = root / name
        if p.is_dir() and not tree_deny_present(p):
            problems.append(f"{name}: 缺 (OI)(CI)(DE,DC) 继承拒删 ACE")
    for name in file_names:
        p = root / name
        if p.is_file() and not has_delete_deny(p):
            problems.append(f"{name}: 缺 (DE) 拒删 ACE")
    return problems
