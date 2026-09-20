"""烧录编排: 枚举 -> 病毒扫描 -> Office转PDF -> 逐文件加密 -> 清单
-> 拷双平台查看器 -> 复检 -> 只读化。

burn() 只做编排, 加密细节在 crypto.py, 转换在 office_convert.py,
扫描在 av_scan.py; 卷序列号通过 serial_getter 注入, 测试不需要真实 U 盘。
"""

from __future__ import annotations

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

from src.app.usb_vault import av_scan, config, inspector, office_convert, unlock_code
from src.app.usb_vault.config import DATA_DIR_NAME, KEYINFO_NAME, MANIFEST_NAME
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

    双入口模式用点前缀名 (.图书阅览器): Mac Finder 默认不显示点前缀项 =>
    分区1上的 Windows 入口对 Mac 收件人不可见; 单分区默认流保持原名零改动。
    与 VIEWER_MAC_BUILD_NAME 同一模式: 产物名不变, 落盘改名。
    """
    if opts.dual_entry:
        return config.VIEWER_WIN_DIR_NAME_DOTTED, config.VIEWER_EXE_NAME_DOTTED
    return config.VIEWER_DIR_NAME, config.VIEWER_EXE_NAME


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
    viewer_exe: Path | None = None  # Windows 查看器 (exe=onefile / 目录=onedir); None=dist 查找+自动构建
    viewer_mac: Path | None = None  # macOS 查看器 .app 目录树; None=dist 查找, 缺失仅告警
    converter: str | None = None  # Office 转换后端 (None=平台默认; 见 office_convert.BACKENDS)
    av_cmd: str | None = None  # 自定义病毒扫描引擎模板 ({path} 占位)
    skip_scan: bool = False  # 显式豁免烧录前病毒扫描 (设计 13.1 fail-closed 的唯一后门)
    use_password: bool = False  # 密码模式: 每盘独立解锁码参与密钥派生 (合同⑤)
    code_archive: Path | None = None  # 解锁码归档文件 (None=secrets/unlock_codes.jsonl)
    require_root: bool = True  # 测试环境用 tmp 目录时传 False
    set_label: bool = False
    apply_readonly: bool = True
    # 双入口分区模式 (由 burn_dual 编排注入, 需 Windows 烧录机 + 管理员):
    # 分区1 = 数据 + Windows 查看器 (点前缀目录名), 分区2 = 仅 Mac 查看器
    dual_entry: bool = False
    dual_scheme: str = config.DUAL_SCHEME_GPT  # 'gpt'=交付级 / 'mbr'=快速验证
    mac_partition_root: Path | None = None  # 分区2临时盘符根; dual_entry 时 .app 写这里


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
    viewer_mac_used: Path | None = None
    av_engine: str | None = None  # 本次烧录前扫描所用引擎; None=未扫 (--skip-scan)
    av_duration_s: float | None = None
    unlock_code: str | None = None  # 本盘解锁码 (密码模式); 非密码模式为 None
    code_archive: Path | None = None  # 归档文件路径 (密码模式)
    dual_entry: bool = False  # 双入口分区模式 (README "双分区烧录")
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
    for exe_name in (config.VIEWER_EXE_NAME, config.VIEWER_EXE_NAME_DOTTED):
        exe_dst = drive / exe_name
        if exe_dst.is_file():
            extras.append(exe_dst)
    for dirname in (config.VIEWER_DIR_NAME, config.VIEWER_WIN_DIR_NAME_DOTTED,
                    config.VIEWER_MAC_DIR_NAME):
        d = drive / dirname
        if d.is_dir():
            extras += _iter_files(d)
    files = _iter_files(data_dir) + extras  # data_dir 缺失时 walk 为空, 不报错
    warnings = set_read_only(data_dir, also=extras)
    if sys.platform == "win32":
        hide_targets: list[Path] = [keyinfo, drive / MANIFEST_NAME, data_dir]
        hide_targets += _iter_files(data_dir)
        for dirname in (config.VIEWER_DIR_NAME, config.VIEWER_WIN_DIR_NAME_DOTTED):
            internal = drive / dirname / "_internal"
            if internal.is_dir():  # onedir 查看器的依赖树; exe 本体保持可见
                hide_targets.append(internal)
        for p in hide_targets:
            if not set_hidden(p):
                warnings.append(f"隐藏失败: {p.name}")
    return warnings, len(files)


def _clear_own_artifacts(drive: Path, *, mac_root: Path | None = None) -> None:
    """重烧前清掉旧产物 (旧密文不残留)。只动 usb_vault 自己的文件。

    查看器名双份常驻兼容 (图书阅览器 与 .图书阅览器): 单/双入口模式互切
    重烧、旧版盘升级都能清干净; mac_root (双入口的分区2临时根) 下同样只清
    自己的目录, 其余一概不动。
    """
    for name in (KEYINFO_NAME, MANIFEST_NAME, config.VIEWER_EXE_NAME,
                 config.VIEWER_EXE_NAME_DOTTED):
        p = drive / name
        if p.exists():
            _make_writable(p)
            p.unlink()
    for root in (drive, mac_root):
        if root is None:
            continue
        for dirname in (DATA_DIR_NAME, config.VIEWER_DIR_NAME,
                        config.VIEWER_WIN_DIR_NAME_DOTTED, config.VIEWER_MAC_DIR_NAME):
            d = root / dirname
            if d.exists():
                for f in _iter_files(d):
                    _make_writable(f)
                shutil.rmtree(d, ignore_errors=True)


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
            f"查看器产物复读校验失败: {dst} "
            f"(源 {len(src_map)} 文件, 盘上 {len(dst_map)} 文件)"
        )


def seed_fingerprint(seed: bytes) -> str:
    """SEED 指纹 (sha256 前 12 hex): 供双机人肉核对, 不暴露 SEED 本体。"""
    return hashlib.sha256(seed).hexdigest()[:12]


def _check_embedded_seed(viewer: Path, embedded_rels: tuple[str, ...], seed: bytes,
                         label: str, *, strict: bool = False) -> None:
    """烧录前读出查看器产物内嵌 seed.bin 与本机 SEED 比对。

    2026-09 真机案例: Mac 构建目录缺 seed.bin 时 build-viewer 会静默生成
    全新 SEED, 产出的 .app 内嵌错钥匙, 在 Mac 上表现恒为"仅限指定的U盘使用"。
    strict (mac .app, 结构里必有): 缺文件即失败; 否则 (win onefile 无独立文件) 跳过。
    """
    p = next((viewer / rel for rel in embedded_rels
              if (viewer / rel).is_file()), None)
    if p is None:
        if strict:
            raise BurnError(
                f"{label}查看器结构异常: 缺 {' 或 '.join(embedded_rels)} "
                f"(不是本工具构建的产物?): {viewer}")
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
            f"macOS 查看器结构异常: 缺 {plist} —— 不是完整的 .app。\n" + _MAC_TRANSIT_FIX)
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
            f"不在 Contents/MacOS/ 里或为空 —— Mac 上双击将无反应。\n" + _MAC_TRANSIT_FIX)


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
    for cand in (dist / config.VIEWER_MAC_DIR_NAME,   # 旧版中文名产物
                 dist / ascii_app,
                 dist / "mac" / config.VIEWER_MAC_DIR_NAME,
                 dist / "mac" / ascii_app):
        if cand.is_dir():
            return cand
    warnings.append(
        "未找到 macOS 查看器 (dist/图书阅览器.app 或 vault_viewer.app); "
        "本盘仅含 Windows 查看器。"
        "需支持 Mac 收件人时: 在 Mac 上 build-viewer 后重烧, 或用 --viewer-mac 指定。"
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

    # ---- 前置检查 -------------------------------------------------------
    if not source.is_dir():
        raise BurnError(f"源目录不存在: {source}")
    if opts.require_root and not is_volume_root(drive):
        raise BurnError(f"目标必须是卷根 (如 E:/), 当前: {drive}")
    if not drive.exists():
        if is_volume_root(drive):  # 形如 F:\ 的卷根不存在 = 盘没插/盘符错
            raise BurnError(
                f"目标盘不存在: {drive} 请插好U盘并核对盘符 "
                "(资源管理器'此电脑', 或 PowerShell 运行 Get-Volume)")
        drive.mkdir(parents=True, exist_ok=True)  # 测试目录则现建
    try:
        serial = serial_getter(drive)
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
    free = shutil.disk_usage(drive).free
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
    office_files = [(f, rel) for f, rel in files
                    if f.suffix.lower() in config.OFFICE_EXTS]
    convert_tmpdir: Path | None = None
    try:
        if office_files and opts.convert_office:
            missing_apps = office_convert.missing_apps(
                [f for f, _ in office_files], opts.converter
            )
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
            seed, license_nonce, serial,
            unlock_code=(unlock_code.derive_component(code) if code is not None else None),
        )

        _clear_own_artifacts(
            drive, mac_root=opts.mac_partition_root if opts.dual_entry else None)
        data_dir = drive / DATA_DIR_NAME
        data_dir.mkdir(parents=True, exist_ok=True)

        entries: list[FileEntry] = []
        used_ids: set[str] = set()
        total_plain = total_enc = 0
        for src_file, rel in files:
            was_converted = src_file in converted
            actual = converted.get(src_file, src_file)
            entry = _encrypt_one(data_key, actual, rel, src_file.suffix.lower(),
                                 data_dir, used_ids, was_converted=was_converted)
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
    (drive / KEYINFO_NAME).write_bytes(
        inspector.build_keyinfo(license_nonce, password_mode=code is not None))
    (drive / MANIFEST_NAME).write_bytes(encrypt_manifest(manifest, data_key))

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

    viewer_mac_used = _resolve_mac_viewer(opts, warnings)
    if viewer_mac_used is not None:
        _check_mac_app_structure(viewer_mac_used)  # 先验骨架 (SEED 对但骨架坏照样打不开)
        _check_embedded_seed(
            viewer_mac_used,
            ("Contents/Frameworks/seed.bin",   # PyInstaller .app 常规落点
             "Contents/Resources/seed.bin",    # 个别版本/配置的备选
             "Contents/MacOS/seed.bin"),
            seed, "macOS", strict=True)
        mac_dst = ((opts.mac_partition_root / config.VIEWER_MAC_DIR_NAME)
                   if opts.dual_entry and opts.mac_partition_root is not None
                   else drive / config.VIEWER_MAC_DIR_NAME)
        _copy_artifact(viewer_mac_used, mac_dst)
        _verify_artifact(viewer_mac_used, mac_dst)

    # ---- 复检 -------------------------------------------------------------
    report = inspector.verify_drive(drive, seed, serial_getter=serial_getter,
                                    unlock_code=code)
    verify_ok = report.all_ok
    if not verify_ok:
        bad = "; ".join(f"{r.rel_path}: {r.error or '哈希不符'}" for r in report.failed)
        raise BurnError(f"烧录后复检未通过, 请换盘或重试: {bad}")

    # ---- 只读化 -----------------------------------------------------------
    if opts.apply_readonly:
        extras = [drive / KEYINFO_NAME, drive / MANIFEST_NAME]
        for exe_name in (config.VIEWER_EXE_NAME, config.VIEWER_EXE_NAME_DOTTED):
            exe_dst = drive / exe_name
            if exe_dst.is_file():
                extras.append(exe_dst)
        for dirname in (config.VIEWER_DIR_NAME, config.VIEWER_WIN_DIR_NAME_DOTTED,
                        config.VIEWER_MAC_DIR_NAME):
            d = drive / dirname
            if d.is_dir():
                extras += _iter_files(d)  # onedir/.app 逐文件只读
        if opts.dual_entry and opts.mac_partition_root is not None:
            p2_app = opts.mac_partition_root / config.VIEWER_MAC_DIR_NAME
            if p2_app.is_dir():
                extras += _iter_files(p2_app)  # 分区2上的 .app 同样只读 (不隐藏)
        warnings += set_read_only(data_dir, also=extras)

    # ---- 隐藏密文载体 (默认视图不可见; 观感层防线, 失败仅告警不阻断) -------
    if sys.platform == "win32":
        hide_targets: list[Path] = [drive / KEYINFO_NAME, drive / MANIFEST_NAME,
                                    data_dir]
        hide_targets += _iter_files(data_dir)
        for dirname in (config.VIEWER_DIR_NAME, config.VIEWER_WIN_DIR_NAME_DOTTED):
            internal = drive / dirname / "_internal"
            if internal.is_dir():  # onedir 查看器的依赖树; exe 本体保持可见
                hide_targets.append(internal)
        for p in hide_targets:
            if not set_hidden(p):
                warnings.append(f"隐藏失败 (加密与盘绑定不受影响, 仅文件名可见): {p.name}")

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
        av_engine=av_engine,
        av_duration_s=av_duration,
        unlock_code=code,
        code_archive=archive_path,
        dual_entry=opts.dual_entry,
        warnings=warnings,
    )


def burn_dual(
    source: Path,
    drive: Path,
    seed: bytes | None = None,
    opts: BurnOptions | None = None,
    *,
    serial_getter: SerialGetter = get_volume_serial,
    confirm_wipe: Callable[[DiskSummary], bool] | None = None,
) -> BurnReport:
    """双入口分区烧录编排: provision/refresh 分区2 → burn() 到分区1 → finalize。

    磁盘操作全在 disk_layout (Windows 烧录机 + 管理员); 分区1 的序列号绑定与
    加密链路与单分区完全一致 —— provision 发生在 burn() 读序列号之前, 新
    分区1 的 exFAT 序列号天然进入密钥派生。既有双入口布局的重烧只刷新
    分区2 (绝不 clean, 无需确认); 全新/异构布局才清盘重建, 且必须过
    confirm_wipe 人工确认 (None = 直接拒绝, 不带确认跑 = 脚本误操作的保险丝)。
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

    # 双入口模式就是为携带 Mac 查看器存在的: 没有产物直接拒烧 (防烧出空分区2)
    probe_warnings: list[str] = []
    mac_viewer = _resolve_mac_viewer(opts, probe_warnings)
    if mac_viewer is None:
        raise BurnError("双入口模式需要 macOS 查看器: 用 --viewer-mac 指定, "
                        "或把 图书阅览器.app 产物放进 dist/")
    opts.viewer_mac = mac_viewer  # burn() 内二次解析直接命中, 不重复告警

    app_bytes = sum(f.stat().st_size for f in _iter_files(mac_viewer))
    p2_mib = disk_layout.p2_size_mib(app_bytes)

    scheme = opts.dual_scheme
    if scheme not in config.DUAL_SCHEMES:
        raise BurnError(f"[dual-entry] 未知双入口制式: {scheme!r} "
                        f"(可选 {config.DUAL_SCHEMES})")

    pre_warnings: list[str] = []
    try:
        layout = disk_layout.inspect_layout(summary.disk_number)
        if layout.dual_scheme == scheme:
            # 既有同制式双入口布局: 只刷新分区2, 绝不 clean, 无需再确认。
            # 旧版 (0x17) MBR 盘顺带迁移: 类型字节改回 0x07, 否则挂不出卷根。
            p2_root = disk_layout.refresh_partition2(
                summary.disk_number, migrate_legacy_mbr=layout.legacy_hidden_p2)
            pre_warnings.append("检测到既有双入口布局, 复用分区 (未清盘)")
            if layout.legacy_hidden_p2:
                pre_warnings.append("旧版 0x17 分区已迁移为 0x07 "
                                    "(macOS 按标准 exFAT 挂载)")
        else:
            if confirm_wipe is None or not confirm_wipe(summary):
                raise BurnError("已取消: 重新分区将全盘清空, 需操作者确认")
            p2_root = disk_layout.provision_dual_layout(summary.disk_number,
                                                        letter, p2_mib,
                                                        scheme=scheme)
    except disk_layout.DiskLayoutError as exc:
        raise BurnError(f"[dual-entry] {exc}") from exc

    # 分区2空间预检 (复用布局才可能不足; 新建布局已按 .app 尺寸预留)
    need = app_bytes + config.PART2_FREE_MARGIN_MIB * 1024 * 1024
    free = shutil.disk_usage(p2_root).free
    if free < need:
        raise BurnError(
            f"第2分区空间不足 (需 {need // (1024 * 1024)} MiB, "
            f"剩 {free // (1024 * 1024)} MiB): 该盘分区2过小 —— 先在资源管理器"
            "把盘格式化回单分区, 再用 --dual-entry 重烧初始化")

    opts.dual_entry = True
    opts.mac_partition_root = p2_root
    try:
        p2_letter = disk_layout._letter_of(p2_root)
    except disk_layout.DiskLayoutError as exc:
        raise BurnError(f"[dual-entry] 分区2根不是盘符路径: {p2_root} ({exc})") from exc
    try:
        report = burn(source, drive, seed, opts, serial_getter=serial_getter)
    except BaseException:
        # 失败也归还临时盘符, 不给系统残留怪异挂载点; 原样重抛
        try:
            disk_layout.finalize_partition2(summary.disk_number, p2_letter,
                                            scheme=scheme)
        except disk_layout.DiskLayoutError:
            pass  # 清理失败不叠加: 主错误优先; 残留盘符由下次 refresh 复用兜底
        raise
    set_volume_label(p2_root, config.PART2_LABEL)  # 观感项, 失败不阻断
    try:
        disk_layout.finalize_partition2(summary.disk_number, p2_letter,
                                        scheme=scheme)
    except disk_layout.DiskLayoutError as exc:
        report.warnings.append(f"[dual-entry] 分区2盘符未能隐藏: {exc}")
    if scheme == config.DUAL_SCHEME_GPT:
        report.warnings.append("分区2已设 GPT 无盘符属性 (写盘上, 换 Windows 机"
                               "也不分配盘符); macOS 按标准 GPT+exFAT 挂载")
    else:
        report.warnings.append("分区2已摘除盘符 (MBR 快速验证制式): 仅烧录机上"
                               "可靠隐藏, 其他 Windows 机首插可能自动补盘符")
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
            f"解锁码归档失败: {exc}; 请立即手工记录本盘解锁码: "
            f"{unlock_code.display(code)}") from exc
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
