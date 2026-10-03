"""烧录编排: 枚举 -> 病毒扫描 -> Office转PDF -> 逐文件加密 -> 清单
-> 拷双平台查看器 -> 复检 -> 只读化。

burn() 只做编排, 加密细节在 crypto.py, 转换在 office_convert.py,
扫描在 av_scan.py; 卷序列号通过 serial_getter 注入, 测试不需要真实 U 盘。
"""

from __future__ import annotations

import base64
import hashlib
import os
import plistlib
import secrets as pysecrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from src.app.usb_vault import av_scan, config, inspector, ntfs_guard, office_convert, unlock_code
from src.app.usb_vault.config import (
    DATA_DIR_NAME,
    DATA_DIR_NAME_LEGACY,
    KEYINFO_NAME,
    KEYINFO_NAME_LEGACY,
    MANIFEST_NAME,
    MANIFEST_NAME_LEGACY,
)
from src.app.usb_vault.crypto import CryptoError, derive_data_key, encrypt_stream
from src.app.usb_vault.manifest import FileEntry, Manifest, encrypt_manifest, now_iso
from src.app.usb_vault.seed import get_or_create_seed
from src.app.usb_vault.volume_id import (
    get_drive_type,
    get_volume_fs,
    get_volume_serial,
    is_volume_root,
    set_hidden,
    set_volume_label,
)

SerialGetter = Callable[[Path], int]

if TYPE_CHECKING:  # 仅类型标注用, 不进 burner 的运行时导入图
    from src.app.usb_vault.disk_layout import DiskSummary

_FAT32_MAX_FILE = 4 * 1024 * 1024 * 1024 - 64 * 1024  # 预留头/标签开销
_EXT_PREFIX = chr(92) * 2 + "?" + chr(92)  # \\?\ 长路径前缀


class BurnError(Exception):
    """烧录失败 (前置检查不过 / 写入 / 校验失败)。"""


def extended_path(p: Path) -> str:
    """超过 MAX_PATH 时加 Windows 扩展长度前缀 (双反斜杠问号), 否则原样返回。"""
    s = str(p)
    if os.name == "nt" and len(s) >= 248 and not s.startswith(_EXT_PREFIX):
        return _EXT_PREFIX + s
    return s


def _normalize_root(drive: Path) -> Path:
    """把 "E:" 这类无锚点盘符表达归一为 "E:/" (卷根)。"""
    if drive.drive and not drive.anchor:
        return Path(drive.drive + "/")
    return drive


def _win_viewer_names(opts: BurnOptions) -> tuple[str, str]:
    """Windows 查看器落盘名 (目录名, exe名)。

    双入口模式用书名前缀 + （适配Windows系统）后缀名 (2026-09-23 用户
    三次定稿: 收件人一眼可辨平台入口与内容, 点前缀隐身设计早已弃用);
    单分区默认流保持原名零改动。与 VIEWER_MAC_BUILD_NAME 同一模式:
    产物名不变, 落盘改名。
    """
    if opts.dual_entry:
        return config.VIEWER_WIN_DIR_NAME_DUAL, config.VIEWER_EXE_NAME_DUAL
    return config.VIEWER_DIR_NAME, config.VIEWER_EXE_NAME


def _mac_viewer_name(opts: BurnOptions, *, intel: bool = False) -> str:
    """macOS 查看器落盘名: 双入口按芯片一分为二 (2026-10-02 双架构交付),
    单分区保持原名 (单 app 形态, 无芯片之分)。"""
    if opts.dual_entry:
        return (config.VIEWER_MAC_DIR_NAME_DUAL_INTEL if intel
                else config.VIEWER_MAC_DIR_NAME_DUAL_APPLE)
    return config.VIEWER_MAC_DIR_NAME


# 查看器落盘名全名单 (当前名 + 历史名): 清扫/只读/回收/隐藏各处共用 ——
# 命名已三代更替 (点前缀 → 半角后缀 → 全角后缀 → 适配后缀), 集中定义防漏名单。
_ALL_EXE_NAMES: tuple[str, ...] = (
    config.VIEWER_EXE_NAME,  # 单分区 onefile
    config.VIEWER_EXE_NAME_DUAL,  # 双入口 onefile (适配后缀版, 当前)
    config.VIEWER_EXE_NAME_FULLWIDTH_LEGACY,  # 全角（WIN）后缀版 (二代)
    config.VIEWER_EXE_NAME_SUFFIXED_LEGACY,  # 半角后缀版 (2026-09-23 第一版)
    config.VIEWER_EXE_NAME_DOTTED_LEGACY,  # 点前缀版 (2026-09-22 及之前)
)
_ALL_VIEWER_DIR_NAMES: tuple[str, ...] = (
    config.VIEWER_DIR_NAME,  # 单分区 onedir
    config.VIEWER_WIN_DIR_NAME_DUAL,  # 双入口 (适配后缀版, 当前)
    config.VIEWER_WIN_DIR_NAME_FULLWIDTH_LEGACY,
    config.VIEWER_WIN_DIR_NAME_SUFFIXED_LEGACY,
    config.VIEWER_WIN_DIR_NAME_DOTTED_LEGACY,
    config.VIEWER_MAC_DIR_NAME,  # 单分区 .app
    config.VIEWER_MAC_DIR_NAME_DUAL_APPLE,  # 双入口 .app Apple 芯片版 (当前)
    config.VIEWER_MAC_DIR_NAME_DUAL_INTEL,  # 双入口 .app Intel 版 (当前)
    config.VIEWER_MAC_DIR_NAME_DUAL_LEGACY,  # 双入口单 Mac 入口名 (适配后缀版, 2026-10-02 双架构拆分前)
    config.VIEWER_MAC_DIR_NAME_FULLWIDTH_LEGACY,
    config.VIEWER_MAC_DIR_NAME_SUFFIXED_LEGACY,
)


# ---------------------------------------------------------------------------
# 哈希包装: 加密的同时顺带算 明文/密文 SHA-256, 单遍 IO
# ---------------------------------------------------------------------------


class _HashingReader:
    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh
        self._hasher = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        data = self._fh.read(n)
        self._hasher.update(data)
        return data

    def hexdigest(self) -> str:
        return self._hasher.hexdigest()


class _HashingWriter:
    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh
        self._hasher = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self._hasher.update(data)
        return self._fh.write(data)

    def tell(self) -> int:
        return self._fh.tell()

    def hexdigest(self) -> str:
        return self._hasher.hexdigest()


# ---------------------------------------------------------------------------
# 选项与报告
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BurnOptions:
    title: str = "资料"
    expire: date | None = None
    max_opens: int | None = None
    convert_office: bool = True
    viewer_exe: Path | None = (
        None  # Windows 查看器 (exe=onefile / 目录=onedir); None=dist 查找+自动构建
    )
    viewer_mac: Path | None = None  # macOS 查看器 .app 目录树; None=dist 查找, 缺失仅告警
    # 2026-10-02 双架构: Intel 版 .app (x86_64 瘦构建)。仅双入口模式消费
    # (单分区保持单 app 形态); dual_entry 时缺失由 burn/burn_dual 熔断, 不许
    # 烧出只有一半 Mac 入口的交付盘。
    viewer_mac_intel: Path | None = None
    # 书名引导壳 exe (v2.4.5): 双入口目录版收件人双击的书名程序。None=dist
    # 查找+自动构建 (Windows); 跨机烧录 (Mac) 须 --launcher-exe 指定。
    launcher_exe: Path | None = None
    converter: str | None = None  # Office 转换后端 (None=平台默认; 见 office_convert.BACKENDS)
    av_cmd: str | None = None  # 自定义病毒扫描引擎模板 ({path} 占位)
    skip_scan: bool = False  # 显式豁免烧录前病毒扫描 (设计 13.1 fail-closed 的唯一后门)
    use_password: bool = False  # 密码模式: 每盘独立解锁码参与密钥派生 (合同⑤)
    code_archive: Path | None = None  # 解锁码归档文件 (None=secrets/unlock_codes.jsonl)
    require_root: bool = True  # 测试环境用 tmp 目录时传 False
    set_label: bool = False
    apply_readonly: bool = True
    # 双入口分区模式 (由 burn_dual 编排注入, 需 Windows 烧录机 + 管理员):
    # v2.4.0 反转布局 —— 分区1 (可见, 盘符) = 双阅览器 ((WIN)/(MAC) 后缀名,
    # 2026-09-23 用户定稿; 旧点前缀名仅留作清扫兼容);
    # 分区2 (无盘符, finalize 后不可见) = 加密链全套 (keyinfo/manifest/DATA)
    dual_entry: bool = False
    dual_scheme: str = config.DUAL_SCHEME_GPT  # 'gpt'=交付级 / 'mbr'=快速验证
    chain_partition_root: Path | None = None  # 分区2临时盘符根; dual_entry 时加密链写这里


@dataclass(slots=True)
class BurnReport:
    manifest: Manifest
    drive: Path
    converted: list[str] = field(default_factory=list)  # 已转 PDF 的原文件 rel_path
    office_left_binary: list[str] = field(default_factory=list)  # --no-convert 未转换
    total_plain: int = 0
    total_enc: int = 0
    verify_ok: bool = False
    viewer_exe_used: Path | None = None
    viewer_mac_used: Path | None = None  # Apple 芯片版 (双入口) / 单分区唯一 Mac app
    viewer_mac_intel_used: Path | None = None  # 2026-10-02 双架构: Intel 版 (仅双入口)
    av_engine: str | None = None  # 本次烧录前扫描所用引擎; None=未扫 (--skip-scan)
    av_duration_s: float | None = None
    unlock_code: str | None = None  # 本盘解锁码 (密码模式); 非密码模式为 None
    code_archive: Path | None = None  # 归档文件路径 (密码模式)
    dual_entry: bool = False  # 双入口分区模式 (README "双分区烧录")
    deny_acl_applied: bool = False  # NTFS 防误删 ACL 已施加 (全部目标卷; 2026-09-28)
    warnings: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.manifest.files)


# ---------------------------------------------------------------------------
# 只读化
# ---------------------------------------------------------------------------


def _make_writable(path: Path) -> None:
    try:
        os.chmod(extended_path(path), stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            out.append(Path(dirpath) / name)
    return out


def set_read_only(root: Path, *, also: list[Path] | None = None) -> list[str]:
    """对 root 下所有文件 + also 列出的文件设置只读属性。

    Windows 上清写位即只读属性; 收件人改不了内容, 重烧前由
    _clear_own_artifacts 自动 _make_writable 解除。返回警告列表。
    """
    warnings: list[str] = []
    targets = _iter_files(root) + (also or [])
    for f in targets:
        try:
            os.chmod(extended_path(f), stat.S_IREAD)  # Windows: 清写位=只读属性
        except OSError as exc:
            warnings.append(f"设置只读失败 {f.name}: {exc}")
    return warnings


def lock_drive(drive: Path) -> tuple[list[str], int]:
    """对已烧录的盘补加/恢复"只读+隐藏"属性, 免重烧 (2026-09-15 防拷贝加固)。

    收件人清了属性 / 调试时解开过 / 旧版烧的盘没上锁 —— 在烧录机对盘根
    重跑本命令即可: +R 只读 (全部产物) + +H+S 隐藏 (密文载体, Windows)。
    幂等, 与 burn 结尾的加固步骤同集。返回 (警告列表, 处理文件数)。
    """
    drive = _normalize_root(Path(drive))
    data_dir = drive / DATA_DIR_NAME
    keyinfo = drive / KEYINFO_NAME
    if not keyinfo.is_file():
        raise BurnError(f"该盘不是资料盘 (缺 {KEYINFO_NAME}): {drive}")
    extras = [keyinfo, drive / MANIFEST_NAME]
    for exe_name in _ALL_EXE_NAMES:
        exe_dst = drive / exe_name
        if exe_dst.is_file():
            extras.append(exe_dst)
    for dirname in _ALL_VIEWER_DIR_NAMES:
        d = drive / dirname
        if d.is_dir():
            extras += _iter_files(d)
    files = _iter_files(data_dir) + extras  # data_dir 缺失时 walk 为空, 不报错
    warnings = set_read_only(data_dir, also=extras)
    if sys.platform == "win32":
        hide_targets: list[Path] = [keyinfo, drive / MANIFEST_NAME, data_dir]
        hide_targets += _iter_files(data_dir)
        for dirname in _ALL_VIEWER_DIR_NAMES:
            vdir = drive / dirname
            internal = vdir / "_internal"
            if internal.is_dir():  # onedir 查看器的依赖树
                hide_targets.append(internal)
            real_exe = vdir / config.VIEWER_EXE_NAME  # v2.4.6: 见 burn_dual 同段注释
            if real_exe.is_file() and (vdir / config.VIEWER_INNER_EXE_NAME_DUAL).is_file():
                hide_targets.append(real_exe)
        for p in hide_targets:
            if not set_hidden(p):
                warnings.append(f"隐藏失败: {p.name}")
    if sys.platform == "win32" and ntfs_guard.is_ntfs(drive):
        # NTFS 防误删同集恢复 (2026-09-28): lock 是"补回加固属性"的入口,
        # deny ACE 缺了也在此补 (幂等); 失败仅告警 —— 盘本身可烧可验,
        # 不必像烧录收尾那样熔断
        try:
            _apply_delete_denial(drive, drive, data_dir)
        except BurnError as exc:
            warnings.append(f"{exc.args[0]} (只读/隐藏已恢复, 防误删 ACL 未恢复)")
    return warnings, len(files)


def _clear_own_artifacts(drive: Path, *, chain_root: Path | None = None) -> None:
    """重烧前清掉旧产物 (旧密文不残留)。只动 usb_vault 自己的文件。

    查看器名全名单清扫 (当前名 + 历史名: 单分区原名 / (WIN)(MAC) 后缀名 /
    旧点前缀名): 单/双入口模式互切重烧、旧版盘升级都能清干净。chain_root
    (v2.4.0 反转布局的分区2临时根, 加密链所在) 下同样只清自己的产物;
    旧版布局的遗留 (keyinfo 直接在 drive 根 / .app 在分区2) 也会被两侧
    联合清扫, 其余文件一概不动。
    """
    roots = [drive] + ([chain_root] if chain_root is not None else [])
    for root in roots:
        # 先清防误删 deny (2026-09-28): 带着拒删 ACL 时 unlink/rmtree 会被
        # 内核拒绝。分区根只摘根上非继承 (DC), 非递归 —— 2026-09-29 真机
        # 事故: 从分区根整树 /t 会踩进 System Volume Information (管理员
        # 亦无权, icacls rc=5); 交付树走整树 /t, 根下散文件 (含 +H+S 隐藏
        # 的链文件) 单点摘, 由下方名单逐项负责, 不靠根递归兜底
        _strip_delete_denial(root, recurse=False)
        # v2.4.7 点前缀换代: 同步清旧明文名 (单分区旧盘重烧不留残链 —— 残留
        # 的旧 keyinfo 是别的 SEED 的盐, 留着只会污染 Mac 观感与排障)
        for name in (
            KEYINFO_NAME,
            KEYINFO_NAME_LEGACY,
            MANIFEST_NAME,
            MANIFEST_NAME_LEGACY,
            *_ALL_EXE_NAMES,
        ):
            p = root / name
            if p.exists():
                _strip_delete_denial(p)
                _make_writable(p)
                p.unlink()
        for dirname in (DATA_DIR_NAME, DATA_DIR_NAME_LEGACY, *_ALL_VIEWER_DIR_NAMES):
            d = root / dirname
            if d.exists():
                _strip_delete_denial(d)
                for f in _iter_files(d):
                    _make_writable(f)
                shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# NTFS 防误删 (2026-09-28 用户评审通过, 探针矩阵定案: 两级都拒)
# ---------------------------------------------------------------------------


def _apply_delete_denial(drive: Path, chain_root: Path, data_dir: Path) -> tuple[bool, list[str]]:
    """烧录收尾施加 NTFS 拒删 ACL: 分区根 deny (DC) + 交付树 deny (OI)(CI)(DE,DC)
    + 散文件 deny (DE) —— 配方与实证依据见 ntfs_guard 模块 docstring。

    只在 NTFS 卷上施加 (exFAT 无 ACL): 非 NTFS 记警告不阻断 (单分区烧到
    exFAT 盘是合法场景, 只是防误删缺席)。施加失败 = BurnError 熔断 (与
    分区2卷标同级: NTFS 交付形态下这是防误删的承诺基础, 坏盘不出仓;
    失败残留的半套 deny 由重跑烧录的 _strip_delete_denial 清障自愈)。
    返回 (是否全部目标卷都施加成功, 警告列表)。
    """
    warnings: list[str] = []
    roots: list[tuple[Path, str]] = [(drive, "阅览器卷")]
    if chain_root != drive:
        roots.append((chain_root, "加密链卷"))
    all_applied = True
    for root, label in roots:
        fs = ntfs_guard.fs_name(root)
        if fs.upper() != "NTFS":
            all_applied = False
            warnings.append(
                f"{label}非 NTFS ({fs or '读不出'}): 防误删 ACL 未施加 "
                "(exFAT 无 ACL —— 只读位挡不住 Explorer 删除, 见 README 防得住/防不住)"
            )
            continue
        try:
            ntfs_guard.deny_delete_children(root)
            if root == chain_root:  # 加密链: 密文树 + 根下散链文件
                ntfs_guard.deny_delete_tree(data_dir)
                ntfs_guard.deny_delete_file(chain_root / KEYINFO_NAME)
                ntfs_guard.deny_delete_file(chain_root / MANIFEST_NAME)
            if root == drive:  # 阅览器卷: 双阅览器树 (+ 历史名残留) + 散 exe
                for dirname in _ALL_VIEWER_DIR_NAMES:
                    d = drive / dirname
                    if d.is_dir():
                        ntfs_guard.deny_delete_tree(d)
                for exe_name in _ALL_EXE_NAMES:
                    f = drive / exe_name
                    if f.is_file():
                        ntfs_guard.deny_delete_file(f)
        except ntfs_guard.NtfsGuardError as exc:
            raise BurnError(f"防误删 ACL 施加失败 ({label}): {exc}") from exc
    return all_applied, warnings


def _strip_delete_denial(path: Path, *, recurse: bool = True) -> None:
    """重烧/清扫前的清障: 摘掉本工具施加的防误删 deny (整树 /t, 探针试验C
    实证配方 —— 单文件 /remove:d 对继承 ACE 会返回成功但实际没摘)。
    recurse=False 供分区根 (只带非继承 (DC), 且递归会踩 System Volume
    Information, 见 ntfs_guard.strip_delete_denial docstring)。

    非 NTFS (exFAT 旧盘/非 Windows) 无 ACL, no-op。失败上抛 BurnError:
    摘不掉就动不了盘, 必须让操作者看见根因, 而不是让后续 unlink 报一串
    PermissionError。
    """
    if sys.platform != "win32" or not ntfs_guard.is_ntfs(path):
        return
    try:
        ntfs_guard.strip_delete_denial(path, recurse=recurse)
    except ntfs_guard.NtfsGuardError as exc:
        raise BurnError(f"清除旧防误删 ACL 失败 ({path}): {exc}") from exc


def _volume_fs(root: Path) -> str:
    """卷文件系统名 (烧录侧包装, 供测试注入); 读不出 = ""。"""
    try:
        return get_volume_fs(root)
    except Exception:  # noqa: BLE001 —— 判据类读不出按 "" 处理 (走重建)
        return ""


def _fs_not_ntfs(root: Path, which: str, warnings_out: list[str]) -> bool:
    """复用判据之三 (2026-09-28 换格式=换代): 任一分区非 NTFS (含读不出/RAW)
    即判全盘重建 —— 沿"换名=换代"同一先例: exFAT 旧形态盘没有防误删 ACL,
    不能借复用路径一直留存。原因写入 warnings_out。"""
    fs = _volume_fs(root)
    if fs.upper() == "NTFS":
        return False
    warnings_out.append(
        f"{which}为 {fs or '读不出'} (2026-09-28 前的 exFAT 形态, 换格式=换代): "
        "全盘重建为 NTFS 交付形态"
    )
    return True


# ---------------------------------------------------------------------------
# 查看器产物解析与字节级复读 (设计 D3: 一只盘同时携带 Win/Mac 查看器)
# ---------------------------------------------------------------------------


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(extended_path(p), "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_artifact(src: Path, dst: Path) -> None:
    """拷贝查看器产物 (单文件 onefile 或整棵目录树 onedir/.app)。"""
    if src.is_file():
        _make_writable(dst)  # 重烧残留
        shutil.copyfile(src, dst)
    else:
        shutil.copytree(src, dst, dirs_exist_ok=True)


def _verify_artifact(src: Path, dst: Path) -> None:
    """拷贝后复读校验: 单文件比对 sha256; 目录树逐文件比对 {相对路径: sha256}。"""
    if src.is_file():
        if not dst.is_file() or _sha256_file(src) != _sha256_file(dst):
            raise BurnError(f"查看器产物复读校验失败: {dst}")
        return
    src_map = {f.relative_to(src).as_posix(): _sha256_file(f) for f in _iter_files(src)}
    dst_map = {f.relative_to(dst).as_posix(): _sha256_file(f) for f in _iter_files(dst)}
    if src_map != dst_map:
        raise BurnError(
            f"查看器产物复读校验失败: {dst} (源 {len(src_map)} 文件, 盘上 {len(dst_map)} 文件)"
        )


def _resolve_dual_launcher(opts: BurnOptions, warnings: list[str]) -> Path | None:
    """解析书名引导壳 exe (v2.4.5): 指定 → dist 查找 → Windows 自动构建。"""
    if opts.launcher_exe is not None:
        if not opts.launcher_exe.is_file():
            raise BurnError(f"指定的书名引导壳不存在: {opts.launcher_exe}")
        return opts.launcher_exe
    dist = config.MODULE_DIR / "dist"
    exe_name = f"{config.VIEWER_LAUNCHER_BUILD_NAME}.exe"
    for cand in (dist / exe_name, dist / "win" / exe_name):
        if cand.is_file():
            return cand
    if sys.platform != "win32":
        return None  # Mac 烧录机无法构建; 缺壳熔断由 _install_dual_launcher 调用处给出
    try:
        from src.app.usb_vault.viewer import build_viewer
    except ImportError:
        return None
    warnings.append("未找到书名引导壳, 正在自动构建 (需要 PyInstaller)...")
    try:
        return build_viewer.build_launcher(dist)
    except build_viewer.BuildError as exc:
        raise BurnError(f"书名引导壳自动构建失败: {exc}") from exc


def _install_dual_launcher(viewer_dst: Path, launcher_src: Path) -> None:
    """把书名引导壳拷入双入口目录并改书名 (v2.4.5)。

    收件人双击的书名 exe (VIEWER_INNER_EXE_NAME_DUAL) = 自包含 onefile 引导
    壳: 目录完整时转交旁边的真主程序 (VIEWER_EXE_NAME, 保持构建名不再改
    名), 残缺 (书名 exe 被单独拷走) 时弹 config.DETACHED_MESSAGE —— onedir
    真主程序被单独拷走会死在 PyInstaller C 装载器层 ("Failed to load Python
    DLL"), Python 尚未启动, v2.4.4 应用层门禁没有机会跑, 壳是唯一能把中文
    谢幕文案送到屏幕上的层 (2026-09-23 用户复测曝光, 根治)。
    先决条件 (load-bearing): 目录里必须有构建名真主程序, 壳按名转交; 缺失 =
    转交链断裂 (盘上双击书名只会弹谢幕框), 熔断而非告警。
    """
    real_exe = viewer_dst / config.VIEWER_EXE_NAME
    if not real_exe.is_file():
        raise BurnError(
            f"目录版查看器缺构建名主程序 {config.VIEWER_EXE_NAME}, "
            f"书名引导壳无从转交 (自定义产物请保持 PyInstaller --name 不变)"
        )
    final_exe = viewer_dst / config.VIEWER_INNER_EXE_NAME_DUAL
    if final_exe.exists():  # 重烧残留 (旧版改名留下的真主程序 / 前次引导壳)
        _make_writable(final_exe)
        final_exe.unlink()
    _copy_artifact(launcher_src, final_exe)
    if _sha256_file(launcher_src) != _sha256_file(final_exe):
        raise BurnError(f"书名引导壳拷贝复读校验失败: {final_exe}")


def seed_fingerprint(seed: bytes) -> str:
    """SEED 指纹 (sha256 前 12 hex): 供双机人肉核对, 不暴露 SEED 本体。"""
    return hashlib.sha256(seed).hexdigest()[:12]


def _check_embedded_seed(
    viewer: Path, embedded_rels: tuple[str, ...], seed: bytes, label: str, *, strict: bool = False
) -> None:
    """烧录前读出查看器产物内嵌 seed.bin 与本机 SEED 比对。

    2026-09 真机案例: Mac 构建目录缺 seed.bin 时 build-viewer 会静默生成
    全新 SEED, 产出的 .app 内嵌错钥匙, 在 Mac 上表现恒为"仅限指定的U盘使用"。
    strict (mac .app, 结构里必有): 缺文件即失败; 否则 (win onefile 无独立文件) 跳过。
    """
    p = next((viewer / rel for rel in embedded_rels if (viewer / rel).is_file()), None)
    if p is None:
        if strict:
            raise BurnError(
                f"{label}查看器结构异常: 缺 {' 或 '.join(embedded_rels)} "
                f"(不是本工具构建的产物?): {viewer}"
            )
        return
    embedded = p.read_bytes()
    if embedded == seed:
        return
    raise BurnError(
        f"{label}查看器内嵌 SEED 与烧录机 SEED 不一致 —— 该查看器解不开本机烧的盘!\n"
        f"  烧录机 SEED 指纹: {seed_fingerprint(seed)}\n"
        f"  查看器内嵌指纹:   {seed_fingerprint(embedded)}\n"
        f"  修复: 把烧录机 {config.SEED_PATH} 拷到构建机同位置, 两边\n"
        f"  sha256 核对同值后重新 build-viewer, 再重烧本盘。"
    )


_MAC_TRANSIT_FIX = (
    "  常见原因: .app 从 Mac 传到烧录机的路上被打坏 —— zip 文件名被按本地码页\n"
    "  改写(中文名 .app 的典型事故)、拷贝中断、或杀毒软件隔离了部分文件。\n"
    "  修复: 在 Mac 上用最新代码重新 build-viewer (产物 vault_viewer.app 内部\n"
    "  全 ASCII 文件名, zip 传输对任何解压工具都安全), zip 拷到烧录机解开后\n"
    "  放进 dist/ 重烧; 旧版中文名 .app 只能用 exFAT U盘直插 Mac 访达整目录拷。\n"
    "  烧录时本工具会把目录以中文名 图书阅览器.app 写到盘上。"
)


def _check_mac_app_structure(app: Path) -> None:
    """烧录前验证 .app 结构完好 (2026-09 真机案例: SEED 一致但 .app 在 Mac 上
    双击无反应 —— 经 Windows 中转时内部文件被打坏, 字节复读再准也已废)。

    只认 Info.plist 声明的可执行文件确实躺在 Contents/MacOS/ 里且非空。
    """
    plist = app / "Contents" / "Info.plist"
    if not plist.is_file():
        raise BurnError(
            f"macOS 查看器结构异常: 缺 {plist} —— 不是完整的 .app。\n" + _MAC_TRANSIT_FIX
        )
    try:
        info = plistlib.loads(plist.read_bytes())
    except Exception as exc:  # plist 损坏形式多样: InvalidFile/ExpatError/解码错
        raise BurnError(
            f"macOS 查看器结构异常: Info.plist 无法解析 ({exc}): {plist}\n" + _MAC_TRANSIT_FIX
        ) from exc
    exe_name = str(info.get("CFBundleExecutable") or "")
    exe = app / "Contents" / "MacOS" / exe_name
    if not exe_name or not exe.is_file() or exe.stat().st_size == 0:
        raise BurnError(
            f"macOS 查看器结构异常: Info.plist 声明的可执行文件 '{exe_name}' "
            f"不在 Contents/MacOS/ 里或为空 —— Mac 上双击将无反应。\n" + _MAC_TRANSIT_FIX
        )


# Mach-O 嵌入签名 SuperBlob 魔数 (CSMAGIC_EMBEDDED_SIGNATURE): cdhash 类封条
# 条目的底线检查用 —— 嵌套已签名代码 (dylib/.so) 的完整性靠各自嵌入签名保证。
_SUPERBLOB_MAGIC = bytes.fromhex("fade0cc0")
# Mach-O 魔数 (32/64 位 + 字节序反转 + fat/universal): cdhash 条目按 codesign
# 语义分型用 —— 只对真 Mach-O 要求嵌入签名魔数 (2026-09-29(c) 真机: codesign
# 对 Frameworks 顶层的非 Mach-O 数据文件同样落 cdhash 条目且不写入嵌入签名,
# base_library.zip/seed.bin 实证与 Resources 侧 hash2 锚定的原件逐字节相同)。
_MACHO_MAGICS = tuple(bytes.fromhex(h) for h in (
    "feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca"))


def _is_macho(raw: bytes) -> bool:
    return raw[:4] in _MACHO_MAGICS


def _check_mac_app_seal(app: Path) -> None:
    """烧录前把 .app 文件树与签名封条 (_CodeSignature/CodeResources) 逐文件比对。

    2026-09-25 真机定谳 (当日二次修正): 新版 PyInstaller 的 .app 布局含大量
    符号链接 (实测 54 处), Info-ZIP `zip -r` 打包默认跟随链接成副本、exFAT
    又无链接语义 —— 中转/烧录后封条逐条登记的链接位必失守, 收件人 Mac 弹
    「已损坏, 无法打开」; 骨架检查与 SEED 比对全部放行。封条级比对
    (plist + sha1/sha256) 在烧录机 Windows 上即可完成, 让这类产物上不了盘。

    封条条目三种形态都要认 (只认 hash 会大面积误报):
    - 符号链接: 旧版 codesign 记 "link", 新版记 "symlink" —— 盘上须真是链接;
    - 嵌套已签名代码 (dylib/.so): 只有 cdhash/requirement 无内容哈希, 改验
      文件内 Mach-O 嵌入签名魔数 (完整校验在收件人 Mac);
    - 普通文件: sha1/sha256 逐字节比对, 缺失仅 optional 条目放行。

    允许的封条外文件与 codesign 口径对齐: Info.plist 与主程序 (各自有独立
    签名通道)、嵌套框架自身的 _CodeSignature、顶层 CodeResources (装订票据)、
    .DS_Store 与 ._xxx AppleDouble 残留 (codesign 官方忽略清单)。树里若存在
    真实符号链接, 即使与封条一致也熔断 —— exFAT 承载不了, 烧了必坏。
    """
    contents = app / "Contents"
    seal_path = contents / "_CodeSignature" / "CodeResources"
    if not seal_path.is_file():
        raise BurnError(
            f"macOS 查看器与签名封条不符: 缺 {seal_path} —— 产物没有签名封条\n"
            "  (未签名, 或封条在中转中丢失)。本工具构建的产物最差也带 ad-hoc 封条,\n"
            "  没有封条的一律不许上盘。\n"
            "  修复: Mac 上用最新代码重新 build-viewer 产物齐套后重烧。"
        )
    try:
        seal = plistlib.loads(seal_path.read_bytes())
    except Exception as exc:
        raise BurnError(
            f"macOS 查看器与签名封条不符: 封条无法解析 ({exc}): {seal_path}"
        ) from exc

    problems: list[str] = []
    sealed: set[str] = set()
    sealed_bundles: list[str] = []  # cdhash 目录条目 = 嵌套代码包 (.framework 等)
    for group in ("files", "files2"):
        for rel, val in dict(seal.get(group) or {}).items():
            entry = val if isinstance(val, dict) else {"hash": val}
            sealed.add(rel)
            p = contents / rel
            # 符号链接条目: 旧版 codesign 记 "link", 新版记 "symlink"
            # (2026-09-25(b): 新版 PyInstaller 布局 54 处链接, 只认 link 键会把
            #  被中转拍平的链接误报成"缺失/哈希不符")
            link_target = entry.get("link", entry.get("symlink"))
            if link_target is not None:
                if isinstance(link_target, bytes):
                    link_target = link_target.decode("utf-8", "replace")
                if not p.is_symlink():
                    problems.append(
                        f"符号链接失守: {rel} (封条要求链接→{link_target}, "
                        "盘上是普通文件/目录)"
                    )
                continue
            if p.is_dir():
                # 嵌套代码包条目 (2026-09-29(c) 真机定谳: 扁平化后的
                # Python.framework 在封条里是一条 cdhash 目录条目, 包内文件
                # 登记在包自己的内封条, 外封条不含): 要求目录在位 + 包内必有
                # _CodeSignature/CodeResources (扁平框架在包根, 版本化在
                # Versions/<v>/Resources/ 下); 子树整体视为已封, 多余扫描跳过。
                has_inner = any(
                    (q / "CodeResources").is_file()
                    for q in p.rglob("_CodeSignature") if q.is_dir())
                if not has_inner:
                    problems.append(f"嵌套代码包缺内封条: {rel}")
                sealed_bundles.append(rel)
                continue
            if not p.is_file():
                if not entry.get("optional"):
                    problems.append(f"封条文件缺失: {rel}")
                continue
            raw = p.read_bytes()
            if "hash" not in entry and "hash2" not in entry:
                # 嵌套已签名代码条目 (cdhash/requirement): 内容哈希不在封条里
                # (完整性由各自嵌入签名保证), 完整校验在收件人 Mac。烧录机侧
                # 底线检查按 codesign 语义分两型 (2026-09-29(c) 真机: codesign
                # 对 Frameworks 顶层的非 Mach-O 数据文件同样落 cdhash 条目且
                # 不写入嵌入签名 —— base_library.zip/seed.bin 与 Resources 侧
                # hash2 锚定原件逐字节相同, 一律强求魔数必误报):
                # - Mach-O (含 fat/universal) → 必须内嵌签名 SuperBlob 魔数;
                # - 非 Mach-O 数据 → 在位即可 (cdhash 完整性收件人 Mac 验)。
                if _is_macho(raw) and _SUPERBLOB_MAGIC not in raw:
                    problems.append(f"嵌套签名代码异常 (无嵌入签名魔数): {rel}")
                continue
            ok = False
            for key, algo in (("hash2", "sha256"), ("hash", "sha1")):
                want = entry.get(key)
                if isinstance(want, str):  # 手写/异构封条可能落 base64 文本
                    want = base64.b64decode(want)
                if want and hashlib.new(algo, raw).digest() == want:
                    ok = True
                    break
            if not ok:
                problems.append(f"哈希不符: {rel}")

    try:
        exe_name = str(plistlib.loads(
            (contents / "Info.plist").read_bytes()).get("CFBundleExecutable") or "")
    except Exception:
        exe_name = ""
    allowed_extra = {"Info.plist", "CodeResources", f"MacOS/{exe_name}"}
    if exe_name:
        # 主程序不在封条哈希清单里 (独立签名通道), 却是外层签名+装订票据的
        # 载体 —— 底线查一次 Mach-O 嵌入签名魔数, 中转损坏当在此拦下。
        exe_path = contents / "MacOS" / exe_name
        exe_raw = exe_path.read_bytes() if exe_path.is_file() else b""
        if exe_raw and _is_macho(exe_raw) and _SUPERBLOB_MAGIC not in exe_raw:
            problems.append(f"主程序无嵌入签名: MacOS/{exe_name}")
    for p in contents.rglob("*"):
        if p.is_symlink():
            # 树里存在真实符号链接: 就算与封条一致, 烧到 exFAT (无链接语义) 也必失守
            problems.append(
                f"产物含符号链接 (exFAT 无法承载): {p.relative_to(contents).as_posix()}"
            )
            continue
        # ._xxx 是 AppleDouble 中转残留 (macOS xattr 摆渡件), .DS_Store 是 Finder
        # 垃圾 —— codesign 官方忽略清单, 上盘前亦无意义
        if not p.is_file() or p.name == ".DS_Store" or p.name.startswith("._"):
            continue
        rel = p.relative_to(contents).as_posix()
        if rel in sealed or rel in allowed_extra:
            continue
        if any(rel == b or rel.startswith(b + "/") for b in sealed_bundles):
            continue  # 嵌套代码包子树: 由包自己的内封条负责
        if "_CodeSignature" in p.relative_to(contents).parts:  # 嵌套框架自身封条
            continue
        problems.append(f"封条外多余文件: {rel}")

    if problems:
        shown = "\n  ".join(problems[:6])
        more = f"\n  …共 {len(problems)} 处" if len(problems) > 6 else ""
        raise BurnError(
            "macOS 查看器与签名封条不符 —— 这份 .app 上盘后收件人 Mac 必弹\n"
            f"「已损坏，无法打开」:\n  {shown}{more}\n"
            "  常见根因 (2026-09-25 真机定谳): 新版 PyInstaller 布局含大量符号链接\n"
            "  (zip 打包会跟随成副本 / exFAT 无链接语义), 中转后封条必对不上;\n"
            "  或 Mac dist 在签名之后又被改动。两条都是 Mac 侧整链重走:\n"
            "  ①同步最新代码 (build-viewer 签名前自动去链接化) → rm -rf dist build\n"
            "  → build-viewer --sign → 公证 → staple → 立即打包传烧录机;\n"
            "  ②烧录机解压到全新空目录后重烧 (本闸门兜底)。"
        )


def _resolve_win_viewer(opts: BurnOptions, seed: bytes, warnings: list[str]) -> Path | None:
    """解析 Windows 查看器: exe(onefile) 或 目录(onedir, 杀毒适配默认, 设计 14.2)。"""
    if opts.viewer_exe is not None:
        if not opts.viewer_exe.exists():
            raise BurnError(f"指定的查看器不存在: {opts.viewer_exe}")
        return opts.viewer_exe
    dist = config.MODULE_DIR / "dist"
    for cand in (
        dist / config.VIEWER_DIR_NAME,  # onedir 默认
        dist / config.VIEWER_EXE_NAME,  # onefile 兼容旧产物
        dist / "win" / config.VIEWER_DIR_NAME,
        dist / "win" / config.VIEWER_EXE_NAME,
    ):
        if cand.exists():
            return cand
    if sys.platform == "darwin":
        # Mac 上自动构建只会产出 mac .app, 塞进 Windows 槽位 = 盘对 Win 收件人不可用
        warnings.append(
            "在 Mac 上无法构建 Windows 查看器 —— 用 --viewer-exe 指定从烧录机"
            "拷来的 Windows 产物 (dist/图书阅览器 目录或 .exe); "
            "目录版双入口还需 --launcher-exe 指定 dist/"
            f"{config.VIEWER_LAUNCHER_BUILD_NAME}.exe (v2.4.5 书名引导壳)。"
            "缺省则本盘仅含 macOS 查看器。"
        )
        return None
    try:
        from src.app.usb_vault.viewer import build_viewer
    except ImportError:
        warnings.append(
            "未找到查看器产物且 PyInstaller 不可用; 烧录的盘没有查看器, "
            "收件人将无法查阅。安装: uv pip install pyinstaller, 或用 --viewer-exe 指定。"
        )
        return None
    warnings.append("未找到现成查看器, 正在自动构建 (需要 PyInstaller)...")
    return build_viewer.build(config.MODULE_DIR / "dist", seed)


def _resolve_mac_viewer(opts: BurnOptions, warnings: list[str]) -> Path | None:
    """解析 macOS 查看器 (.app 目录树); 缺失仅告警不阻断 —— 需真 Mac 构建 (设计 D3)。"""
    if opts.viewer_mac is not None:
        if not opts.viewer_mac.is_dir():
            raise BurnError(f"指定的 macOS 查看器目录不存在: {opts.viewer_mac}")
        return opts.viewer_mac
    dist = config.MODULE_DIR / "dist"
    ascii_app = f"{config.VIEWER_MAC_BUILD_NAME}.app"  # 新版 ASCII 产物名
    for cand in (
        dist / config.VIEWER_MAC_DIR_NAME,  # 旧版中文名产物
        dist / ascii_app,
        dist / "mac" / config.VIEWER_MAC_DIR_NAME,
        dist / "mac" / ascii_app,
    ):
        if cand.is_dir():
            return cand
    warnings.append(
        "未找到 macOS 查看器 (dist/图书阅览器.app 或 vault_viewer.app); "
        "本盘仅含 Windows 查看器。"
        "需支持 Mac 收件人时: 在 Mac 上 build-viewer 后重烧, 或用 --viewer-mac 指定。"
    )
    return None


def _resolve_mac_viewer_intel(opts: BurnOptions, warnings: list[str]) -> Path | None:
    """解析 Intel 版 macOS 查看器 (2026-10-02 双架构交付)。

    与 Apple 版 (vault_viewer.app) 同一套检查 (骨架/SEED/封条) 在 burn() 主流程
    逐 app 施加; 这里只定位来源。单分区模式不消费 (单 app 形态无芯片之分)。
    """
    if opts.viewer_mac_intel is not None:
        if not opts.viewer_mac_intel.is_dir():
            raise BurnError(f"指定的 Intel 版 macOS 查看器目录不存在: {opts.viewer_mac_intel}")
        return opts.viewer_mac_intel
    dist = config.MODULE_DIR / "dist"
    intel_app = f"{config.VIEWER_MAC_BUILD_NAME_INTEL}.app"
    for cand in (
        dist / intel_app,
        dist / "mac-intel" / config.VIEWER_MAC_BUILD_NAME,
    ):
        if cand.is_dir():
            return cand
    warnings.append(
        f"未找到 Intel 版 macOS 查看器 (dist/{intel_app}); "
        "双入口交付盘要求双架构 Mac 入口。"
        "在 Mac 上用 arch -x86_64 venv 再跑一次 build-viewer 后, "
        "改名 vault_viewer_intel.app 放进 dist/, 或用 --viewer-mac-intel 指定。"
    )
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def burn(
    source: Path,
    drive: Path,
    seed: bytes | None = None,
    opts: BurnOptions | None = None,
    *,
    serial_getter: SerialGetter = get_volume_serial,
) -> BurnReport:
    """把 source 目录加密烧录到 drive 卷根。"""
    opts = opts or BurnOptions()
    seed = seed if seed is not None else get_or_create_seed()
    source = Path(source)
    drive = _normalize_root(Path(drive))
    # v2.4.0 反转布局: 加密链 (keyinfo/manifest/DATA) 全套落在 chain_root
    # (双入口 = 分区2临时根; 单分区 = drive 本身)。序列号绑定因此锚定在
    # 加密链所在卷 —— 双入口盘即分区2, 与阅览器卷解耦。
    chain_root = (
        drive
        if opts.chain_partition_root is None
        else _normalize_root(Path(opts.chain_partition_root))
    )

    # ---- 前置检查 -------------------------------------------------------
    if not source.is_dir():
        raise BurnError(f"源目录不存在: {source}")
    if opts.require_root and not is_volume_root(drive):
        raise BurnError(f"目标必须是卷根 (如 E:/), 当前: {drive}")
    if opts.require_root and chain_root is not drive and not is_volume_root(chain_root):
        raise BurnError(f"加密链目标必须是卷根 (如 K:/), 当前: {chain_root}")
    if not drive.exists():
        if is_volume_root(drive):  # 形如 F:\ 的卷根不存在 = 盘没插/盘符错
            raise BurnError(
                f"目标盘不存在: {drive} 请插好U盘并核对盘符 "
                "(资源管理器'此电脑', 或 PowerShell 运行 Get-Volume)"
            )
        drive.mkdir(parents=True, exist_ok=True)  # 测试目录则现建
    try:
        serial = serial_getter(chain_root)
    except Exception as exc:  # VolumeIdError 等
        raise BurnError(f"读取目标卷序列号失败: {exc}") from exc

    warnings: list[str] = []
    try:
        dtype = get_drive_type(drive)
        if dtype != "removable":
            warnings.append(f"目标不是可移动盘 ({dtype}), 请确认没选错盘")
        fs = get_volume_fs(drive)
    except Exception as exc:  # noqa: BLE001 前置信息类失败不阻断
        fs = ""
        warnings.append(f"读取卷信息失败 (继续): {exc}")

    files = _iter_source_files(source)
    if not files:
        raise BurnError(f"源目录没有文件: {source}")

    total_source = sum(f.stat().st_size for f, _ in files)
    free = shutil.disk_usage(chain_root).free
    if total_source + 128 * 1024 * 1024 > free:
        raise BurnError(
            f"目标空间可能不足: 需约 {total_source / 1e6:.1f}MB+, 剩余 {free / 1e6:.1f}MB"
        )
    if fs.upper() == "FAT32":
        for f, _rel in files:
            if f.stat().st_size > _FAT32_MAX_FILE:
                raise BurnError(f"FAT32 单文件 4GiB 上限, 超限: {f.name}")

    # ---- 病毒扫描 (设计 13.1): 明文阶段是唯一可扫时机, fail-closed ------
    av_engine: str | None = None
    av_duration: float | None = None
    if opts.skip_scan:
        warnings.append("已按 --skip-scan 跳过烧录前病毒扫描, 由操作者自担风险")
    else:
        try:
            outcome = av_scan.scan_tree(source, av_cmd=opts.av_cmd)
        except av_scan.AVUnavailable as exc:
            raise BurnError(
                f"病毒扫描引擎不可用, 已中止烧录 (fail-closed): {exc}。"
                "安装/修复引擎后重试; 确认无风险时可用 --skip-scan 显式豁免"
            ) from exc
        except av_scan.AVScanError as exc:
            raise BurnError(
                f"病毒扫描执行失败, 已中止烧录 (fail-closed): {exc}。"
                "排查后重试, 或 --skip-scan 显式豁免"
            ) from exc
        if not outcome.clean:
            names = "; ".join(dict.fromkeys(outcome.threats))
            raise BurnError(f"源目录检出威胁, 已中止烧录: {names}")
        av_engine, av_duration = outcome.engine, outcome.duration_s

    # ---- Office -> PDF ---------------------------------------------------
    converted: dict[Path, Path] = {}
    office_left_binary: list[str] = []
    office_files = [(f, rel) for f, rel in files if f.suffix.lower() in config.OFFICE_EXTS]
    convert_tmpdir: Path | None = None
    try:
        if office_files and opts.convert_office:
            missing_apps = office_convert.missing_apps([f for f, _ in office_files], opts.converter)
            if missing_apps:
                names = ", ".join(sorted({rel for _, rel in office_files}))
                raise BurnError(
                    f"后端 {opts.converter or office_convert.default_backend()} "
                    f"缺少组件 {', '.join(sorted(missing_apps))}, 无法转换 Office 文档: "
                    f"[{names}]。安装 Office, 或手动转 PDF 后重烧, 或加 --no-convert (仅元数据展示)"
                )
            # 注意: 转出的 PDF 要活到下面加密完, 不能用 with TemporaryDirectory
            convert_tmpdir = Path(tempfile.mkdtemp(prefix="usb_vault_convert_"))
            converted = office_convert.convert_batch(
                [f for f, _ in office_files], convert_tmpdir, backend=opts.converter
            )
        elif office_files:
            office_left_binary = [rel for _, rel in office_files]

        # ---- 加密写入 ---------------------------------------------------------
        license_nonce = os.urandom(16)
        code = unlock_code.generate() if opts.use_password else None
        data_key = derive_data_key(
            seed,
            license_nonce,
            serial,
            unlock_code=(unlock_code.derive_component(code) if code is not None else None),
        )

        _clear_own_artifacts(drive, chain_root=chain_root if chain_root is not drive else None)
        data_dir = chain_root / DATA_DIR_NAME
        data_dir.mkdir(parents=True, exist_ok=True)

        entries: list[FileEntry] = []
        used_ids: set[str] = set()
        total_plain = total_enc = 0
        for src_file, rel in files:
            was_converted = src_file in converted
            actual = converted.get(src_file, src_file)
            entry = _encrypt_one(
                data_key,
                actual,
                rel,
                src_file.suffix.lower(),
                data_dir,
                used_ids,
                was_converted=was_converted,
            )
            entries.append(entry)
            total_plain += entry.size
            total_enc += entry.enc_size
    finally:
        # 转换临时目录的 PDF 已全部加密入盘, 立即清理
        if convert_tmpdir is not None:
            shutil.rmtree(convert_tmpdir, ignore_errors=True)

    manifest = Manifest(
        version=1,
        title=opts.title,
        created_at=now_iso(),
        expire=opts.expire.isoformat() if opts.expire else None,
        max_opens=opts.max_opens,
        files=sorted(entries, key=lambda e: e.rel_path),
        av_scanned=({"engine": av_engine, "time": now_iso()} if av_engine else None),
    )
    (chain_root / KEYINFO_NAME).write_bytes(
        inspector.build_keyinfo(license_nonce, password_mode=code is not None)
    )
    (chain_root / MANIFEST_NAME).write_bytes(encrypt_manifest(manifest, data_key))

    # ---- 查看器 (Windows + macOS 双产物, 设计 D3) -------------------------
    viewer_used = _resolve_win_viewer(opts, seed, warnings)
    viewer_dst: Path | None = None
    if viewer_used is not None:
        if viewer_used.is_dir():  # onedir: 内嵌 seed.bin 可直接读出比对
            _check_embedded_seed(viewer_used, ("_internal/seed.bin",), seed, "Windows")
        viewer_dir_name, viewer_exe_name = _win_viewer_names(opts)
        viewer_dst = drive / (viewer_dir_name if viewer_used.is_dir() else viewer_exe_name)
        _copy_artifact(viewer_used, viewer_dst)
        _verify_artifact(viewer_used, viewer_dst)
        if viewer_used.is_dir() and opts.dual_entry:
            launcher = _resolve_dual_launcher(opts, warnings)
            if launcher is None:
                raise BurnError(
                    "双入口目录版需要书名引导壳 (v2.4.5, 单拷书名 exe 的中文谢幕"
                    "文案靠它送达): Windows 上重跑 build-viewer / burn 会自动构建; "
                    "Mac 烧录机请从 Windows 构建机拷 dist/"
                    f"{config.VIEWER_LAUNCHER_BUILD_NAME}.exe, 用 --launcher-exe 指定。"
                )
            _install_dual_launcher(viewer_dst, launcher)

    # 2026-10-02 双架构: Mac 入口按芯片一分为二。Apple 版 (主 app) 缺失沿旧制
    # 仅告警 (单分区/单 Mac 形态合法); 双入口交付盘必须两架构齐备 —— Intel 版
    # 缺失直接熔断, 不烧"只有一半 Mac 收件人能打开"的盘。
    mac_apps: list[tuple[Path, bool]] = []  # (源 .app, 是否 Intel 版)
    apple_app = _resolve_mac_viewer(opts, warnings)
    if apple_app is not None:
        mac_apps.append((apple_app, False))
    if opts.dual_entry:
        intel_app = _resolve_mac_viewer_intel(opts, warnings)
        if intel_app is None:
            raise BurnError(
                "双入口模式需要双架构 macOS 查看器 (Apple 芯片 + Intel): "
                "用 --viewer-mac-intel 指定, 或把 "
                f"{config.VIEWER_MAC_BUILD_NAME_INTEL}.app 放进 dist/"
            )
        mac_apps.append((intel_app, True))
    viewer_mac_used: Path | None = None
    viewer_mac_intel_used: Path | None = None
    for src, is_intel in mac_apps:
        _check_mac_app_structure(src)  # 先验骨架 (SEED 对但骨架坏照样打不开)
        _check_embedded_seed(
            src,
            (
                "Contents/Frameworks/seed.bin",  # PyInstaller .app 常规落点
                "Contents/Resources/seed.bin",  # 个别版本/配置的备选
                "Contents/MacOS/seed.bin",
            ),
            seed,
            "macOS",
            strict=True,
        )
        # 封条级比对 (最后, 骨架+SEED 都对仍可能整体失守): 拦"签名之后又被改动/
        # 混入另一次构建产物"的缝合 .app —— 2026-09-25 真机: 封条只认 Frameworks/
        # 一棵树, 盘上却多了 Resources/ 整棵, 收件人 Mac 弹「已损坏, 无法打开」。
        # 两个架构版各自走一遍 (缝合/损坏不分会带哪个芯片的壳)。
        _check_mac_app_seal(src)
        # v2.4.0 反转布局: .app 与 Windows 阅览器同在分区1 (可见卷);
        # 2026-09-23 用户定稿: 双入口落盘名收件人一眼可辨; 2026-10-02 起 Mac
        # 入口按芯片一分为二, 收件人按「关于本机 → 芯片/处理器」对号入座
        mac_dst = drive / _mac_viewer_name(opts, intel=is_intel)
        _copy_artifact(src, mac_dst)
        _verify_artifact(src, mac_dst)
        if is_intel:
            viewer_mac_intel_used = src
        else:
            viewer_mac_used = src

    # ---- 复检 (对加密链所在卷做, 序列号/内容一致性同链路) -------------------
    report = inspector.verify_drive(chain_root, seed, serial_getter=serial_getter, unlock_code=code)
    verify_ok = report.all_ok
    if not verify_ok:
        bad = "; ".join(f"{r.rel_path}: {r.error or '哈希不符'}" for r in report.failed)
        raise BurnError(f"烧录后复检未通过, 请换盘或重试: {bad}")

    # ---- 只读化 -----------------------------------------------------------
    if opts.apply_readonly:
        extras = [chain_root / KEYINFO_NAME, chain_root / MANIFEST_NAME]
        for exe_name in _ALL_EXE_NAMES:
            exe_dst = drive / exe_name
            if exe_dst.is_file():
                extras.append(exe_dst)
        for dirname in _ALL_VIEWER_DIR_NAMES:
            d = drive / dirname
            if d.is_dir():
                extras += _iter_files(d)  # onedir/.app 逐文件只读 (v2.4.0 全在分区1)
        warnings += set_read_only(data_dir, also=extras)

    # ---- 隐藏密文载体 (默认视图不可见; 观感层防线, 失败仅告警不阻断) -------
    if sys.platform == "win32":
        hide_targets: list[Path] = [chain_root / KEYINFO_NAME, chain_root / MANIFEST_NAME, data_dir]
        hide_targets += _iter_files(data_dir)
        for dirname in _ALL_VIEWER_DIR_NAMES:
            vdir = drive / dirname
            internal = vdir / "_internal"
            if internal.is_dir():  # onedir 查看器的依赖树
                hide_targets.append(internal)
            # v2.4.6: 双入口目录内真主程序 +H+S —— 它顶着一代旧名 (图书阅览器.exe)
            # 裸露在书名 exe 旁, 2026-09-23 真棒事故里烧录后被当旧版残留清掉, 引导壳
            # 无从转交。藏到与 _internal 同待遇; 判据 = 书名引导壳在旁 (单分区 onedir
            # 的同名 exe 是唯一可见入口, 不在藏匿之列)。
            real_exe = vdir / config.VIEWER_EXE_NAME
            if real_exe.is_file() and (vdir / config.VIEWER_INNER_EXE_NAME_DUAL).is_file():
                hide_targets.append(real_exe)
        for p in hide_targets:
            if not set_hidden(p):
                warnings.append(f"隐藏失败 (加密与盘绑定不受影响, 仅文件名可见): {p.name}")

    # ---- NTFS 防误删 (2026-09-28): 两级 deny ACL, 配方见 ntfs_guard -------
    # exFAT 单分区合法场景记警告缺席; NTFS 卷施加失败熔断 (坏盘不出仓)
    deny_ok, deny_warns = _apply_delete_denial(drive, chain_root, data_dir)
    warnings += deny_warns

    if opts.set_label:
        label = opts.title[:11] if fs.upper() == "FAT32" else opts.title[:32]
        set_volume_label(drive, label)

    # ---- 解锁码归档 (密码模式; 复检通过后才记, 烧废的码不进档案) -----------
    archive_path: Path | None = None
    if code is not None:
        archive_path = _archive_code(opts.code_archive, code, opts.title, serial)

    return BurnReport(
        manifest=manifest,
        drive=drive,
        converted=sorted(rel for f, rel in office_files if f in converted),
        office_left_binary=office_left_binary,
        total_plain=total_plain,
        total_enc=total_enc,
        verify_ok=verify_ok,
        viewer_exe_used=viewer_used,
        viewer_mac_used=viewer_mac_used,
        viewer_mac_intel_used=viewer_mac_intel_used,
        av_engine=av_engine,
        av_duration_s=av_duration,
        unlock_code=code,
        code_archive=archive_path,
        dual_entry=opts.dual_entry,
        deny_acl_applied=deny_ok,
        warnings=warnings,
    )


# 复用路径分区1尺寸检查的容忍差额: exFAT 卷容量略小于分区尺寸属正常
_P1_FLOOR_SLACK_MIB = 8


def _p1_below_target(drive: Path, p1_mib: int, warnings_out: list[str]) -> bool:
    """复用判据之二 (2026-09-22 用户定稿: 分区1底线 1 GiB): 分区1是拔插
    可见可读的唯一入口, 底线抬高前烧出的旧小分区1不能借复用路径一直留存
    (2026-09-22 复盘: 复用只看分区2内容不看分区1尺寸, 旧 542 MiB 盘重烧
    会静默保持小分区1)。低于本轮应给尺寸 (含容量都读不出的 RAW 卷) 即判
    重建扩容, 原因写入 warnings_out。"""
    try:
        total_mib = shutil.disk_usage(drive).total // (1024 * 1024)
    except OSError as exc:
        warnings_out.append(f"分区1容量读不出 ({exc}), 视为不达标: 全盘重建")
        return True
    if total_mib + _P1_FLOOR_SLACK_MIB < p1_mib:
        warnings_out.append(
            f"分区1仅 {total_mib} MiB, 低于本轮应给 {p1_mib} MiB "
            f"(底线 {config.VIEWER_PART_MIN_MIB} MiB): 全盘重建扩容"
        )
        return True
    return False


def burn_dual(
    source: Path,
    drive: Path,
    seed: bytes | None = None,
    opts: BurnOptions | None = None,
    *,
    serial_getter: SerialGetter = get_volume_serial,
    confirm_wipe: Callable[[DiskSummary], bool] | None = None,
) -> BurnReport:
    """双入口分区烧录编排 (v2.4.0 反转布局): provision/refresh 分区2 →
    burn() (双阅览器→分区1, 加密链→分区2) → finalize 隐藏分区2。

    磁盘操作全在 disk_layout (Windows 烧录机 + 管理员)。序列号绑定锚定在
    加密链所在的分区2, 与单分区链路完全一致 —— provision 发生在 burn()
    读序列号之前, 新分区2 的 NTFS 序列号 (@0x48, 2026-09-28 起双分区均
    NTFS) 天然进入密钥派生。既有反转布局的重烧只刷新分区2 (绝不 clean),
    判据按内容 (挂出后见 keyinfo) —— 但分区1尺寸低于本轮应给 (含 1 GiB
    底线, 2026-09-22 用户定稿) 或任一分区非 NTFS (2026-09-28 换格式=换代)
    时改为清盘重建; 旧版布局 (分区2=.app) / 全新 / 异构盘同样清盘重建。
    凡清盘必过 confirm_wipe 人工确认 (None = 直接拒绝, 不带确认跑 = 脚本
    误操作的保险丝)。
    """
    if sys.platform != "win32":
        raise BurnError("双入口分区烧录仅在 Windows 烧录机执行")
    from src.app.usb_vault import disk_layout  # 函数体内导入: 测试 monkeypatch 接缝

    if opts is None:
        opts = BurnOptions()
    drive = _normalize_root(Path(drive))
    try:
        disk_layout.require_admin()
        letter = disk_layout._letter_of(drive)
        if not drive.exists():
            raise BurnError(f"目标盘不存在: {drive}")
        summary = disk_layout.resolve_disk(letter)
    except disk_layout.DiskLayoutError as exc:
        raise BurnError(f"[dual-entry] {exc}") from exc

    # 双入口模式就是为携带 Mac 查看器存在的: 没有产物直接拒烧 (防烧出空分区2);
    # 2026-10-02 双架构: Mac 入口按芯片一分为二, Apple + Intel 两版都要在场
    # (分区编排之前拦下, 不烧一半 Mac 收件人打不开的盘)
    probe_warnings: list[str] = []
    mac_viewer = _resolve_mac_viewer(opts, probe_warnings)
    if mac_viewer is None:
        raise BurnError(
            "双入口模式需要 macOS 查看器: 用 --viewer-mac 指定, 或把 图书阅览器.app 产物放进 dist/"
        )
    opts.viewer_mac = mac_viewer  # burn() 内二次解析直接命中, 不重复告警
    mac_viewer_intel = _resolve_mac_viewer_intel(opts, probe_warnings)
    if mac_viewer_intel is None:
        raise BurnError(
            "双入口模式需要 Intel 版 macOS 查看器 (双架构交付): "
            "在 Mac 上用 arch -x86_64 venv 跑一次 build-viewer 后改名 "
            f"{config.VIEWER_MAC_BUILD_NAME_INTEL}.app 放进 dist/, "
            "或用 --viewer-mac-intel 指定。"
        )
    opts.viewer_mac_intel = mac_viewer_intel

    # 分区1按双阅览器总体积定宽 (v2.4.0 反转布局: 分区1=阅览器, 分区2=加密链;
    # 2026-10-02 起 Mac 入口 ×2); Windows 阅览器提前解析并回填 opts,
    # burn() 内二次解析直接命中不重复告警
    if seed is None:
        seed = get_or_create_seed()
    win_viewer = _resolve_win_viewer(opts, seed, probe_warnings)
    if win_viewer is not None:
        opts.viewer_exe = win_viewer
    viewers_bytes = sum(f.stat().st_size for f in _iter_files(mac_viewer))
    viewers_bytes += sum(f.stat().st_size for f in _iter_files(mac_viewer_intel))
    if win_viewer is not None:
        viewers_bytes += sum(f.stat().st_size for f in _iter_files(win_viewer))
    p1_mib = disk_layout.viewer_part_size_mib(viewers_bytes)

    scheme = opts.dual_scheme
    if scheme not in config.DUAL_SCHEMES:
        raise BurnError(f"[dual-entry] 未知双入口制式: {scheme!r} (可选 {config.DUAL_SCHEMES})")

    pre_warnings: list[str] = []
    try:
        layout = disk_layout.inspect_layout(summary.disk_number)
        p2_root: Path | None = None
        if layout.dual_scheme == scheme:
            # 表签名与内容无关 (新旧布局都是"无盘符分区2"), 挂出来看
            # 内容定去留: 有 keyinfo 且分区1尺寸达标且两分区均 NTFS =
            # 本工具反转布局 → 复用, 绝不 clean; 分区1过小/不可读/非 NTFS
            # → 重建扩容; 否则 (旧版布局分区2=.app / 异构多分区盘) → 全盘重建
            p2_root = disk_layout.refresh_partition2(
                summary.disk_number, migrate_legacy_mbr=layout.legacy_hidden_p2
            )
            if (p2_root / KEYINFO_NAME).is_file():
                if _p1_below_target(drive, p1_mib, pre_warnings) or _fs_not_ntfs(
                    drive, "分区1", pre_warnings
                ) or _fs_not_ntfs(p2_root, "分区2", pre_warnings):
                    # 分区1过小/RAW 或任一分区非 NTFS (2026-09-28 换格式=换代,
                    # 同"换名=换代"先例: exFAT 旧形态没有防误删 ACL) → 清盘重建
                    p2_root = None
                else:
                    pre_warnings.append("检测到既有双入口布局, 复用分区 (未清盘)")
                    if layout.legacy_hidden_p2:
                        pre_warnings.append(
                            "旧版 0x17 分区已迁移为 0x07 (macOS 按标准 exFAT 挂载)"
                        )
            else:
                pre_warnings.append(
                    "分区2内容非本工具加密链 (旧版布局/旧版文件名或异构), 将全盘重建为反转布局"
                )
                p2_root = None
        if p2_root is None:
            if confirm_wipe is None or not confirm_wipe(summary):
                raise BurnError("已取消: 重新分区将全盘清空, 需操作者确认")
            p2_root = disk_layout.provision_dual_layout(
                summary.disk_number, letter, p1_mib, scheme=scheme
            )
    except disk_layout.DiskLayoutError as exc:
        raise BurnError(f"[dual-entry] {exc}") from exc

    # 分区1空间预检 (仅复用路径: 新建布局已按阅览器尺寸定宽; 即将被
    # _clear_own_artifacts 清掉的旧阅览器体积一并计入可用空间)
    if any("复用分区" in w for w in pre_warnings):
        reclaim = 0
        for name in _ALL_VIEWER_DIR_NAMES:
            d = drive / name
            if d.exists():
                reclaim += sum(f.stat().st_size for f in _iter_files(d))
        need = viewers_bytes + config.VIEWER_PART_FREE_MARGIN_MIB * 1024 * 1024
        free = shutil.disk_usage(drive).free + reclaim
        if free < need:
            raise BurnError(
                f"分区1空间不足 (需 {need // (1024 * 1024)} MiB, "
                f"剩 {free // (1024 * 1024)} MiB): 该盘阅览器分区过小 —— 先在"
                "资源管理器把盘格式化回单分区, 再用 --dual-entry 重烧初始化"
            )

    opts.dual_entry = True
    opts.chain_partition_root = p2_root
    try:
        p2_letter = disk_layout._letter_of(p2_root)
    except disk_layout.DiskLayoutError as exc:
        raise BurnError(f"[dual-entry] 分区2根不是盘符路径: {p2_root} ({exc})") from exc
    try:
        report = burn(source, drive, seed, opts, serial_getter=serial_getter)
    except BaseException:
        # 失败也归还临时盘符, 不给系统残留怪异挂载点; 原样重抛
        try:
            disk_layout.finalize_partition2(summary.disk_number, p2_letter, scheme=scheme)
        except disk_layout.DiskLayoutError:
            pass  # 清理失败不叠加: 主错误优先; 残留盘符由下次 refresh 复用兜底
        raise
    # 分区2卷标: v2.4.7 起点前缀 (.资料存储) 即 Mac 侧藏匿机制 —— 落空则
    # macOS 桌面多出一个 Untitled 空卷, 不再是纯观感项; 先走完收尾再熔断,
    # 保证失败路径不残留临时盘符。
    p2_label_ok = set_volume_label(p2_root, config.PART2_LABEL)
    # 分区1卷标: 双入口盘恒落 title (2026-09-23 真机: 未设卷标的 exFAT 在
    # Mac Finder 显示 Untitled / Windows 显示 新加卷, 而收件人手册承诺桌面
    # 出现 图书资料 一个卷名)。交付流程 --title 图书资料 即得正名;
    # --set-label 从此只对单分区模式有开关意义。
    if not set_volume_label(drive, opts.title[:32]):
        report.warnings.append("分区1卷标未能设置 (Mac 侧将显示 Untitled)")
    try:
        disk_layout.finalize_partition2(summary.disk_number, p2_letter, scheme=scheme)
    except disk_layout.DiskLayoutError as exc:
        report.warnings.append(f"[dual-entry] 分区2盘符未能隐藏: {exc}")
    if not p2_label_ok:
        raise BurnError(
            f"分区2卷标未能设置为 {config.PART2_LABEL}: Mac 侧桌面将多出一个 "
            "Untitled 空卷, 藏匿机制失效 —— 请重跑烧录 (复用路径会原位重写)"
        )
    if scheme == config.DUAL_SCHEME_GPT:
        report.warnings.append(
            "分区2 (加密链) 已设 GPT 无盘符属性 (写盘上, 换 "
            "Windows 机也不分配盘符, 资源管理器不显示); "
            "macOS 仍会按标准 GPT 挂载该卷 (2026-09-28 起 NTFS, "
            "预期只读挂载, 收件机表现待真机核验; 卷标 "
            f"{config.PART2_LABEL}, 内容为密文)"
        )
    else:
        report.warnings.append(
            "分区2 (加密链) 已摘除盘符 (MBR 快速验证制式): "
            "仅烧录机上可靠隐藏, 其他 Windows 机首插可能"
            "自动补盘符"
        )
    report.warnings.extend(pre_warnings)
    return report


def _encrypt_one(
    data_key: bytes,
    src: Path,
    rel_path: str,
    orig_suffix: str,
    data_dir: Path,
    used_ids: set[str],
    *,
    was_converted: bool,
) -> FileEntry:
    """加密单个文件到 DATA/<id>.enc, 返回清单条目。"""
    fid = pysecrets.token_hex(16)
    while fid in used_ids:
        fid = pysecrets.token_hex(16)
    used_ids.add(fid)

    plain_len = src.stat().st_size
    dst = data_dir / f"{fid}.enc"
    with open(extended_path(src), "rb") as sf, open(dst, "wb") as df:
        hr = _HashingReader(sf)
        hw = _HashingWriter(df)
        try:
            encrypt_stream(data_key, hr, hw, plain_len=plain_len)
        except CryptoError as exc:
            raise BurnError(f"加密失败 {rel_path}: {exc}") from exc

    media = "pdf" if was_converted else config.classify_media(orig_suffix)
    return FileEntry(
        id=fid,
        rel_path=rel_path,
        size=plain_len,
        enc_size=dst.stat().st_size,
        sha256=hr.hexdigest(),
        enc_sha256=hw.hexdigest(),
        media=media,
        converted_from=orig_suffix.lstrip(".") if was_converted else None,
    )


def _archive_code(path: Path | None, code: str, title: str, serial: int) -> Path:
    """解锁码归档 (合同⑤: 乙方保密备份, 留存≤质保期, 到期由流程销毁)。

    失败即中止并把码带进报错文案 —— 操作者至少能手工记录, 绝不出现
    "盘烧好了但码丢了"的不可逆事故。
    """
    p = path or config.CODE_ARCHIVE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        is_new = not p.exists()
        with open(extended_path(p), "a", encoding="utf-8") as fh:
            fh.write(unlock_code.archive_line(code, title=title, serial=serial) + "\n")
        if is_new:
            os.chmod(extended_path(p), 0o600)
    except OSError as exc:
        raise BurnError(
            f"解锁码归档失败: {exc}; 请立即手工记录本盘解锁码: {unlock_code.display(code)}"
        ) from exc
    return p


def _iter_source_files(source: Path) -> list[tuple[Path, str]]:
    """枚举源目录全部常规文件, 返回 (绝对路径, posix 相对路径), 目录序稳定。"""
    out: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(source).as_posix()
            out.append((p, rel))
    return out
