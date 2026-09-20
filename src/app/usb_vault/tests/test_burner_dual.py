"""burn_dual 双入口编排单测: 有状态 fake disk_layout + P1/P2 tmp 目录。

磁盘操作 (provision/refresh/finalize/resolve) 全部经 monkeypatch 换成内存
状态机; 平台经 sys.platform 注入 win32, CI 双平台可跑。win32 属性真实
断言的用例单独 skipif 限制在 Windows。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import src.app.usb_vault.disk_layout as disk_layout_mod
from src.app.usb_vault import burner, config, inspector
from src.app.usb_vault.burner import BurnOptions, BurnError, burn, burn_dual, lock_drive
from src.app.usb_vault.disk_layout import DiskLayout, DiskSummary, PartitionInfo

pytestmark = pytest.mark.usefixtures("dist_isolated")

SERIAL = 0x11223344


def _serial(_p: Path) -> int:
    return SERIAL


def _make_writable(root: Path) -> None:
    for dirpath, _d, names in os.walk(root):
        for n in names:
            try:
                os.chmod(os.path.join(dirpath, n), stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("内容A", encoding="utf-8")
    (src / "b.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return src


def _write_plist(app: Path) -> None:
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": "图书阅览器"}))


def _fake_mac_app(base: Path, seed: bytes, macho: bytes = b"FAKE-MACHO") -> Path:
    app = base / "图书阅览器.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "图书阅览器").write_bytes(macho)
    _write_plist(app)
    frameworks = app / "Contents" / "Frameworks"
    frameworks.mkdir()
    (frameworks / "seed.bin").write_bytes(seed)
    return app


class _FakeDisk:
    """有状态 disk_layout 替身: 记录 provision/refresh/finalize, 维护当前布局。"""

    def __init__(self, p2_root: Path,
                 scheme: str | None = None, legacy_0x17: bool = False) -> None:
        self.p2_root = p2_root
        self._scheme = scheme  # None=还不是双入口布局
        self._legacy_0x17 = legacy_0x17  # mbr 且分区2为旧版 0x17
        self.provisioned = 0
        self.refreshed = 0
        self.finalized = 0
        self.provision_schemes: list[str] = []
        self.finalize_schemes: list[str] = []
        self.refresh_migrations: list[bool] = []

    def require_admin(self) -> None:
        pass

    def _letter_of(self, _p: Path) -> str:
        return "K"

    def resolve_disk(self, letter: str) -> DiskSummary:
        return DiskSummary(3, "SanDisk Ultra", 32 * 1024**3, "USB", [letter])

    def p2_size_mib(self, viewer_bytes: int) -> int:
        return 256

    def inspect_layout(self, disk_number: int) -> DiskLayout:
        if self._scheme == config.DUAL_SCHEME_GPT:
            parts = [PartitionInfo(1, None, config.GPT_BASIC_DATA_TYPE, True),
                     PartitionInfo(2, None, config.GPT_BASIC_DATA_TYPE, False)]
            return DiskLayout(disk_number, "GPT", parts)
        if self._scheme == config.DUAL_SCHEME_MBR:
            p2_type = (config.PART2_TYPE_HIDDEN_LEGACY if self._legacy_0x17
                       else config.PART2_TYPE_EXFAT)
            parts = [PartitionInfo(1, 0x07, None, True),
                     PartitionInfo(2, p2_type, None, False)]
            return DiskLayout(disk_number, "MBR", parts)
        return DiskLayout(disk_number, "MBR", [PartitionInfo(1, 0x07, None, True)])

    def provision_dual_layout(self, disk_number: int, letter: str, p2_mib: int, *,
                              scheme: str = config.DUAL_SCHEME_GPT) -> Path:
        self.provisioned += 1
        self.provision_schemes.append(scheme)
        self._scheme = scheme
        self._legacy_0x17 = False
        return self.p2_root

    def refresh_partition2(self, disk_number: int, *,
                           migrate_legacy_mbr: bool = False) -> Path:
        self.refreshed += 1
        self.refresh_migrations.append(migrate_legacy_mbr)
        self._legacy_0x17 = False  # 迁移后不再是 0x17
        return self.p2_root

    def finalize_partition2(self, disk_number: int, temp_letter: str, *,
                            scheme: str = config.DUAL_SCHEME_GPT) -> None:
        self.finalized += 1
        self.finalize_schemes.append(scheme)


@pytest.fixture()
def dual_env(monkeypatch: pytest.MonkeyPatch,
             tmp_path: Path) -> Iterator[tuple[_FakeDisk, Path, Path, Path, bytes]]:
    """win32 注入 + disk_layout 全 fake + P1/P2 目录 + 可用的 mac .app。

    返回 (fake, P1, P2, mac_app, seed); 结束时解除两分区上的只读属性。
    """
    monkeypatch.setattr(sys, "platform", "win32")
    p1 = tmp_path / "P1"
    p2 = tmp_path / "P2"
    p1.mkdir()
    p2.mkdir()
    fake = _FakeDisk(p2)
    for name in ("require_admin", "_letter_of", "resolve_disk", "p2_size_mib",
                 "inspect_layout", "provision_dual_layout", "refresh_partition2",
                 "finalize_partition2"):
        monkeypatch.setattr(disk_layout_mod, name, getattr(fake, name))
    # win32 原生属性 API 在非 Windows CI 上不可用: 统一平替
    # (隐藏行为的真实断言在下方 win32-only 用例)
    monkeypatch.setattr(burner, "set_hidden", lambda p: True)
    monkeypatch.setattr(burner, "set_volume_label", lambda root, label: True)
    seed = os.urandom(64)
    mac_app = _fake_mac_app(tmp_path / "mac_dist", seed)
    yield fake, p1, p2, mac_app, seed
    _make_writable(p1)
    _make_writable(p2)


def _dual_opts(tmp_path: Path, mac_app: Path, **kw: object) -> BurnOptions:
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    base: dict[str, object] = {"require_root": False, "convert_office": False,
                               "skip_scan": True, "viewer_exe": viewer,
                               "viewer_mac": mac_app}
    base.update(kw)
    return BurnOptions(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 首烧 / 确认 / 重烧
# ---------------------------------------------------------------------------

def test_dual_first_burn_provisions_copies_and_finalizes(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path) -> None:
    fake, p1, p2, mac_app, seed = dual_env
    confirmed: list[DiskSummary] = []

    def _confirm(s: DiskSummary) -> bool:
        confirmed.append(s)
        return True

    report = burn_dual(source_dir, p1, seed, _dual_opts(p1.parent, mac_app),
                       serial_getter=_serial, confirm_wipe=_confirm)

    assert fake.provisioned == 1 and fake.finalized == 1
    assert fake.provision_schemes == [config.DUAL_SCHEME_GPT]  # 默认交付级制式
    assert fake.finalize_schemes == [config.DUAL_SCHEME_GPT]
    assert confirmed and confirmed[0].disk_number == 3  # 确认提示拿到了磁盘概要
    assert report.dual_entry
    # 分区1: 数据 + 点前缀 Windows 查看器; 不再出现原名目录
    assert (p1 / config.KEYINFO_NAME).is_file()
    assert (p1 / config.DATA_DIR_NAME).is_dir()
    assert (p1 / config.VIEWER_EXE_NAME_DOTTED).is_file()
    assert not (p1 / config.VIEWER_DIR_NAME).exists()
    assert not (p1 / config.VIEWER_MAC_DIR_NAME).exists()  # .app 不落分区1
    # 分区2: 只有 .app
    assert (p2 / config.VIEWER_MAC_DIR_NAME).is_dir()
    assert any("GPT 无盘符属性" in w for w in report.warnings)
    vr = inspector.verify_drive(p1, seed, serial_getter=_serial)
    assert vr.all_ok


def test_dual_burn_requires_mac_viewer(dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
                                       source_dir: Path, tmp_path: Path) -> None:
    fake, p1, _p2, _app, seed = dual_env
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    opts = BurnOptions(require_root=False, convert_office=False, skip_scan=True,
                       viewer_exe=viewer)
    with pytest.raises(BurnError, match="macOS 查看器"):
        burn_dual(source_dir, p1, seed, opts, serial_getter=_serial)
    assert fake.provisioned == 0


def test_dual_burn_without_confirm_callback_refuses(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path) -> None:
    fake, p1, _p2, mac_app, seed = dual_env
    with pytest.raises(BurnError, match="确认"):
        burn_dual(source_dir, p1, seed, _dual_opts(p1.parent, mac_app),
                  serial_getter=_serial)  # confirm_wipe=None: 脚本误操作的保险丝
    assert fake.provisioned == 0


def test_dual_burn_confirm_declined_aborts(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path) -> None:
    fake, p1, _p2, mac_app, seed = dual_env
    (p1 / "用户文件.txt").write_text("不该被动到", encoding="utf-8")
    with pytest.raises(BurnError, match="已取消"):
        burn_dual(source_dir, p1, seed, _dual_opts(p1.parent, mac_app),
                  serial_getter=_serial, confirm_wipe=lambda s: False)
    assert fake.provisioned == 0
    assert (p1 / "用户文件.txt").read_text(encoding="utf-8") == "不该被动到"


def test_dual_reburn_reuses_layout_no_clean_no_prompt(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path, tmp_path: Path) -> None:
    fake, p1, _p2, mac_app, seed = dual_env
    burn_dual(source_dir, p1, seed, _dual_opts(tmp_path, mac_app),
              serial_getter=_serial, confirm_wipe=lambda s: True)
    assert fake._scheme == config.DUAL_SCHEME_GPT

    def no_prompt(_s: DiskSummary) -> bool:
        raise AssertionError("重烧复用布局不应再触发清盘确认")

    mac_app2 = _fake_mac_app(tmp_path / "mac_dist2", seed, macho=b"MACHO-V2")
    report2 = burn_dual(source_dir, p1, seed, _dual_opts(tmp_path, mac_app2),
                        serial_getter=_serial, confirm_wipe=no_prompt)
    assert fake.provisioned == 1 and fake.refreshed == 1 and fake.finalized == 2
    assert any("复用分区" in w for w in report2.warnings)
    p2 = fake.p2_root
    macho = p2 / config.VIEWER_MAC_DIR_NAME / "Contents" / "MacOS" / "图书阅览器"
    assert macho.read_bytes() == b"MACHO-V2"  # 分区2 .app 已刷新


def test_dual_mbr_scheme_passed_through(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path) -> None:
    """--dual-scheme mbr (快速验证制式): provision/finalize 全链带 mbr。"""
    fake, p1, _p2, mac_app, seed = dual_env
    report = burn_dual(source_dir, p1, seed,
                       _dual_opts(p1.parent, mac_app, dual_scheme=config.DUAL_SCHEME_MBR),
                       serial_getter=_serial, confirm_wipe=lambda s: True)
    assert fake.provision_schemes == [config.DUAL_SCHEME_MBR]
    assert fake.finalize_schemes == [config.DUAL_SCHEME_MBR]
    assert any("MBR 快速验证制式" in w for w in report.warnings)


def test_dual_unknown_scheme_refuses(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path) -> None:
    fake, p1, _p2, mac_app, seed = dual_env
    with pytest.raises(BurnError, match="未知双入口制式"):
        burn_dual(source_dir, p1, seed,
                  _dual_opts(p1.parent, mac_app, dual_scheme="bogus"),
                  serial_getter=_serial, confirm_wipe=lambda s: True)
    assert fake.provisioned == 0


def test_dual_legacy_0x17_migrated_on_refresh(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path) -> None:
    """用户手里的旧 0x17 测试盘 (macOS 不挂载的元凶): mbr 制式重烧自动迁回 0x07,
    绝不清盘、无需确认。"""
    fake, p1, _p2, mac_app, seed = dual_env
    fake._scheme = config.DUAL_SCHEME_MBR
    fake._legacy_0x17 = True

    def no_prompt(_s: DiskSummary) -> bool:
        raise AssertionError("旧盘迁移不应触发清盘确认")

    report = burn_dual(source_dir, p1, seed,
                       _dual_opts(p1.parent, mac_app, dual_scheme=config.DUAL_SCHEME_MBR),
                       serial_getter=_serial, confirm_wipe=no_prompt)
    assert fake.provisioned == 0 and fake.refreshed == 1
    assert fake.refresh_migrations == [True]
    assert any("迁移" in w for w in report.warnings)


def test_dual_scheme_mismatch_reprovisions_with_confirm(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path) -> None:
    """盘上是 gpt 布局却要 mbr (或反之): 不复用, 走清盘重建 + 确认。"""
    fake, p1, _p2, mac_app, seed = dual_env
    fake._scheme = config.DUAL_SCHEME_GPT
    confirmed: list[DiskSummary] = []

    def _confirm(s: DiskSummary) -> bool:
        confirmed.append(s)
        return True

    burn_dual(source_dir, p1, seed,
              _dual_opts(p1.parent, mac_app, dual_scheme=config.DUAL_SCHEME_MBR),
              serial_getter=_serial, confirm_wipe=_confirm)
    assert fake.refreshed == 0 and fake.provisioned == 1
    assert fake.provision_schemes == [config.DUAL_SCHEME_MBR]
    assert confirmed and confirmed[0].disk_number == 3


def test_dual_finalize_runs_even_when_burn_fails(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, p1, _p2, mac_app, seed = dual_env

    def boom(*a: object, **kw: object) -> None:
        raise inspector.InspectorError("复检通道炸了")

    monkeypatch.setattr(inspector, "verify_drive", boom)
    with pytest.raises(inspector.InspectorError):
        burn_dual(source_dir, p1, seed, _dual_opts(p1.parent, mac_app),
                  serial_getter=_serial, confirm_wipe=lambda s: True)
    assert fake.finalized == 1  # 失败也归还临时盘符


# ---------------------------------------------------------------------------
# 清理兼容 / 空间 / 回归闸
# ---------------------------------------------------------------------------

def test_dual_clears_both_viewer_names_and_stale_p2_app(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path) -> None:
    fake, p1, p2, mac_app, seed = dual_env
    # 旧单分区盘的产物 (原名目录) + 旧双分区残留 (点前缀 + 分区2旧 .app)
    old_plain = p1 / config.VIEWER_DIR_NAME
    old_plain.mkdir()
    (old_plain / "旧.exe").write_bytes(b"OLD")
    os.chmod(old_plain / "旧.exe", stat.S_IREAD)  # 只读也必须清得掉
    old_p2_app = p2 / config.VIEWER_MAC_DIR_NAME
    (old_p2_app / "Contents" / "MacOS").mkdir(parents=True)
    (old_p2_app / "Contents" / "MacOS" / "图书阅览器").write_bytes(b"STALE")

    burn_dual(source_dir, p1, seed, _dual_opts(p1.parent, mac_app),
              serial_getter=_serial, confirm_wipe=lambda s: True)
    assert not old_plain.exists()  # 原名目录清掉
    assert (p1 / config.VIEWER_EXE_NAME_DOTTED).is_file()
    macho = p2 / config.VIEWER_MAC_DIR_NAME / "Contents" / "MacOS" / "图书阅览器"
    assert macho.read_bytes() == b"FAKE-MACHO"  # 分区2旧内容已换新


def test_dual_p2_space_check_aborts(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, p1, p2, mac_app, seed = dual_env

    real_usage = shutil.disk_usage

    def fake_usage(arg: str | Path, *a: object, **kw: object) -> Any:
        if Path(arg) == p2:
            return SimpleNamespace(free=1024)  # 分区2只剩 1KB
        return real_usage(arg)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    with pytest.raises(BurnError, match="第2分区空间不足"):
        burn_dual(source_dir, p1, seed, _dual_opts(p1.parent, mac_app),
                  serial_getter=_serial, confirm_wipe=lambda s: True)


def test_dual_burn_non_windows_refuses(source_dir: Path, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    seed = os.urandom(64)
    mac_app = _fake_mac_app(tmp_path, seed)
    with pytest.raises(BurnError, match="仅.*Windows"):
        burn_dual(source_dir, tmp_path / "P1", seed, _dual_opts(tmp_path, mac_app),
                  serial_getter=_serial, confirm_wipe=lambda s: True)


def test_single_mode_never_touches_disk_layout(
        source_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单分区默认流的回归闸: 不触碰 disk_layout, 落盘名保持原样。"""

    def boom() -> None:
        raise AssertionError("单分区流不应触碰 disk_layout")

    monkeypatch.setattr(disk_layout_mod, "require_admin", boom)
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    drive = tmp_path / "USB"
    drive.mkdir()
    report = burn(source_dir, drive, b"S" * 64,
                  BurnOptions(require_root=False, viewer_exe=viewer,
                              convert_office=False, skip_scan=True),
                  serial_getter=_serial)
    assert not report.dual_entry
    assert (drive / config.VIEWER_EXE_NAME).is_file()  # 原名, 无点前缀


# ---------------------------------------------------------------------------
# lock_drive / 隐藏属性 (win32 真实断言)
# ---------------------------------------------------------------------------

def test_lock_drive_covers_dotted_dir(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path) -> None:
    fake, p1, _p2, mac_app, seed = dual_env
    burn_dual(source_dir, p1, seed, _dual_opts(p1.parent, mac_app),
              serial_getter=_serial, confirm_wipe=lambda s: True)
    _make_writable(p1)  # 解开烧录时上的只读, lock 再统一补
    warnings, n = lock_drive(p1)
    dotted_exe = p1 / config.VIEWER_EXE_NAME_DOTTED
    assert dotted_exe.is_file()
    assert not dotted_exe.stat().st_mode & stat.S_IWUSR  # 点前缀 exe 也只读


@pytest.mark.skipif(sys.platform != "win32", reason="win32 文件属性真实断言")
def test_dual_hide_dotted_internal_exe_visible(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    from src.app.usb_vault.volume_id import set_hidden as real_set_hidden

    monkeypatch.setattr(burner, "set_hidden", real_set_hidden)
    fake, p1, _p2, mac_app, seed = dual_env
    # onedir Windows 查看器: _internal 依赖树 + 入口 exe
    onedir = tmp_path / "win_dist" / config.VIEWER_DIR_NAME
    internal = onedir / "_internal"
    internal.mkdir(parents=True)
    (internal / "seed.bin").write_bytes(seed)
    (onedir / config.VIEWER_EXE_NAME).write_bytes(b"FAKE-EXE")

    opts = BurnOptions(require_root=False, convert_office=False, skip_scan=True,
                       viewer_exe=onedir, viewer_mac=mac_app)
    burn_dual(source_dir, p1, seed, opts, serial_getter=_serial,
              confirm_wipe=lambda s: True)

    def attrs(p: Path) -> int:
        return int(ctypes.windll.kernel32.GetFileAttributesW(str(p)))

    dotted = p1 / config.VIEWER_WIN_DIR_NAME_DOTTED
    assert dotted.is_dir()
    assert attrs(dotted / "_internal") & 0x6 == 0x6  # 依赖树目录 H+S (整树不可见)
    assert attrs(dotted / "_internal" / "seed.bin") & 0x1 == 0x1  # 内部文件只读
    exe = dotted / config.VIEWER_EXE_NAME
    assert exe.is_file()
    assert not attrs(exe) & 0x2  # 入口 exe 保持可见


@pytest.mark.skipif(sys.platform != "win32", reason="win32 只读位真实断言")
def test_dual_viewer_entries_readonly_set(
        dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
        source_dir: Path) -> None:
    """2026-09-20 防误删评审收口: 双入口两端的阅读器入口 (分区1 点前缀
    exe + 分区2 .app 整树) 全文件 +R。

    **防删声明已撤回 (同日真机+探针双证)**: 资源管理器删除会先剥只读属性
    再删 (单文件/整目录都删得掉); 只读位挡的是改写/覆写与不带强制参数的
    命令行删除, 误删恢复靠备份或重烧。见 test_burner 同名用例的完整声明。
    """
    import ctypes

    fake, p1, p2, mac_app, seed = dual_env
    burn_dual(source_dir, p1, seed, _dual_opts(p1.parent, mac_app),
              serial_getter=_serial, confirm_wipe=lambda s: True)

    def attrs(p: Path) -> int:
        return int(ctypes.windll.kernel32.GetFileAttributesW(str(p)))

    def readonly(p: Path) -> None:
        assert p.exists(), f"产物缺失: {p}"
        a = attrs(p)
        assert a & 0x1 == 0x1, f"缺只读位: {p.name} -> {a:#x}"

    readonly(p1 / config.VIEWER_EXE_NAME_DOTTED)  # 分区1 Windows 入口
    app = p2 / config.VIEWER_MAC_DIR_NAME  # 分区2 macOS 入口
    readonly(app / "Contents" / "MacOS" / "图书阅览器")
    readonly(app / "Contents" / "Info.plist")
    readonly(app / "Contents" / "Frameworks" / "seed.bin")

    # DeleteFileW 直调拒绝只读文件 (改写类防护契约锚点, 非 Explorer 路径)
    with pytest.raises(PermissionError):
        os.remove(p1 / config.VIEWER_EXE_NAME_DOTTED)
    with pytest.raises(PermissionError):
        os.remove(app / "Contents" / "MacOS" / "图书阅览器")
