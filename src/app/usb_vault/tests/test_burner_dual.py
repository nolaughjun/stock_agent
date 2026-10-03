"""burn_dual 双入口编排单测 (v2.4.0 反转布局): 有状态 fake + P1/P2 tmp 目录。

反转布局: 分区1 (可见) = 双阅览器; 分区2 (=fake 的 p2_root) = 加密链全套。
磁盘操作 (provision/refresh/finalize/resolve) 全部经 monkeypatch 换成内存
状态机; 平台经 sys.platform 注入 win32, CI 双平台可跑。win32 属性真实
断言的用例单独 skipif 限制在 Windows。
"""

from __future__ import annotations

import hashlib
import itertools
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
    if sys.platform == "win32" and root.is_dir():
        # 2026-09-28 起烧录收尾施加 NTFS 拒删 ACL (Windows 本机 p1/p2 是真
        # NTFS 目录, icacls 真跑): 解除只读前先整树摘掉, 否则 tmp 清理被拒
        from src.app.usb_vault import ntfs_guard

        if ntfs_guard.is_ntfs(root):
            try:
                ntfs_guard.strip_delete_denial(root)
            except ntfs_guard.NtfsGuardError:
                pass
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
        plistlib.dumps({"CFBundleExecutable": "图书阅览器"})
    )


def _write_seal(app: Path) -> None:
    """伪签名封条: 与真实 codesign 同构 (files 表 + sha1), 供烧录封条闸门放行。"""
    contents = app / "Contents"
    entries: dict[str, bytes] = {}
    for p in sorted(contents.rglob("*")):
        if not p.is_file() or p.name in {"Info.plist", ".DS_Store"}:
            continue
        rel = p.relative_to(contents).as_posix()
        if rel.startswith("MacOS/") or "_CodeSignature" in p.relative_to(contents).parts:
            continue  # 主程序/封条自身不在 files 表里 (真实封条同口径)
        entries[rel] = hashlib.sha1(p.read_bytes()).digest()
    cs = contents / "_CodeSignature"
    cs.mkdir(exist_ok=True)
    (cs / "CodeResources").write_bytes(plistlib.dumps({"files": entries}))


def _fake_mac_app(base: Path, seed: bytes, macho: bytes = b"FAKE-MACHO") -> Path:
    app = base / "图书阅览器.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "图书阅览器").write_bytes(macho)
    _write_plist(app)
    frameworks = app / "Contents" / "Frameworks"
    frameworks.mkdir()
    (frameworks / "seed.bin").write_bytes(seed)
    _write_seal(app)
    return app


class _FakeDisk:
    """有状态 disk_layout 替身: 记录 provision/refresh/finalize, 维护当前布局。"""

    def __init__(self, p2_root: Path, scheme: str | None = None, legacy_0x17: bool = False) -> None:
        self.p2_root = p2_root
        self._scheme = scheme  # None=还不是双入口布局
        self._legacy_0x17 = legacy_0x17  # mbr 且分区2为旧版 0x17
        self.provisioned = 0
        self.refreshed = 0
        self.finalized = 0
        self.provision_schemes: list[str] = []
        self.finalize_schemes: list[str] = []
        self.refresh_migrations: list[bool] = []
        self.p1_sizes: list[int] = []
        self.volume_labels: list[tuple[Path, str]] = []

    def require_admin(self) -> None:
        pass

    def _letter_of(self, _p: Path) -> str:
        return "K"

    def resolve_disk(self, letter: str) -> DiskSummary:
        return DiskSummary(3, "SanDisk Ultra", 32 * 1024**3, "USB", [letter])

    def viewer_part_size_mib(self, viewer_bytes: int) -> int:
        self.p1_sizes.append(viewer_bytes)
        return 256

    def inspect_layout(self, disk_number: int) -> DiskLayout:
        if self._scheme == config.DUAL_SCHEME_GPT:
            parts = [
                PartitionInfo(1, None, config.GPT_BASIC_DATA_TYPE, True),
                PartitionInfo(2, None, config.GPT_BASIC_DATA_TYPE, False),
            ]
            return DiskLayout(disk_number, "GPT", parts)
        if self._scheme == config.DUAL_SCHEME_MBR:
            p2_type = (
                config.PART2_TYPE_HIDDEN_LEGACY if self._legacy_0x17 else config.PART2_TYPE_EXFAT
            )
            parts = [PartitionInfo(1, 0x07, None, True), PartitionInfo(2, p2_type, None, False)]
            return DiskLayout(disk_number, "MBR", parts)
        return DiskLayout(disk_number, "MBR", [PartitionInfo(1, 0x07, None, True)])

    def provision_dual_layout(
        self, disk_number: int, letter: str, p1_mib: int, *, scheme: str = config.DUAL_SCHEME_GPT
    ) -> Path:
        self.provisioned += 1
        self.provision_schemes.append(scheme)
        self._scheme = scheme
        self._legacy_0x17 = False
        return self.p2_root

    def refresh_partition2(self, disk_number: int, *, migrate_legacy_mbr: bool = False) -> Path:
        self.refreshed += 1
        self.refresh_migrations.append(migrate_legacy_mbr)
        self._legacy_0x17 = False  # 迁移后不再是 0x17
        return self.p2_root

    def finalize_partition2(
        self, disk_number: int, temp_letter: str, *, scheme: str = config.DUAL_SCHEME_GPT
    ) -> None:
        self.finalized += 1
        self.finalize_schemes.append(scheme)


@pytest.fixture()
def dual_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[_FakeDisk, Path, Path, Path, bytes]]:
    """win32 注入 + disk_layout 全 fake + P1/P2 目录 + 可用的 mac .app。

    返回 (fake, P1, P2, mac_app, seed); 结束时解除两分区上的只读属性。
    """
    monkeypatch.setattr(sys, "platform", "win32")
    p1 = tmp_path / "P1"
    p2 = tmp_path / "P2"
    p1.mkdir()
    p2.mkdir()
    fake = _FakeDisk(p2)
    for name in (
        "require_admin",
        "_letter_of",
        "resolve_disk",
        "viewer_part_size_mib",
        "inspect_layout",
        "provision_dual_layout",
        "refresh_partition2",
        "finalize_partition2",
    ):
        monkeypatch.setattr(disk_layout_mod, name, getattr(fake, name))
    # win32 原生属性 API 在非 Windows CI 上不可用: 统一平替
    # (隐藏行为的真实断言在下方 win32-only 用例)
    monkeypatch.setattr(burner, "set_hidden", lambda p: True)

    def _set_volume_label(root: Path, label: str) -> bool:
        fake.volume_labels.append((root, label))
        return True

    monkeypatch.setattr(burner, "set_volume_label", _set_volume_label)
    # 2026-09-28 换格式=换代: 复用判据按 NTFS 认; 统一注入, Windows 本机
    # (tmp 在 C: 上本就 NTFS) 与非 Windows CI (读不出会误判 exFAT 旧形态)
    # 同走复用路径, exFAT 换代用例在测试内单独改写
    monkeypatch.setattr(burner, "_volume_fs", lambda root: "NTFS")
    seed = os.urandom(64)
    mac_app = _fake_mac_app(tmp_path / "mac_dist", seed)
    yield fake, p1, p2, mac_app, seed
    _make_writable(p1)
    _make_writable(p2)


# 2026-10-02 双架构: _dual_opts 默认派生一个与 Apple 版同 SEED 的 Intel 假 app
# (burn 对两个 .app 各跑骨架/SEED/封条三查); 缺 Intel / Intel SEED 不符等
# 专门用例显式传 viewer_mac_intel=None 或错误产物覆盖。
_INTEL_SEQ = itertools.count()


def _dual_opts(tmp_path: Path, mac_app: Path, **kw: object) -> BurnOptions:
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"LAUNCHER-SHELL")
    if "viewer_mac_intel" not in kw:
        seed = (mac_app / "Contents" / "Frameworks" / "seed.bin").read_bytes()
        kw["viewer_mac_intel"] = _fake_mac_app(
            tmp_path / f"mac_intel_{next(_INTEL_SEQ)}", seed)
    base: dict[str, object] = {
        "require_root": False,
        "convert_office": False,
        "skip_scan": True,
        "viewer_exe": viewer,
        "viewer_mac": mac_app,
        "launcher_exe": launcher,  # v2.4.5 书名引导壳 (目录版 onedir 需要)
    }
    base.update(kw)
    return BurnOptions(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 首烧 / 确认 / 重烧
# ---------------------------------------------------------------------------


def test_dual_first_burn_provisions_copies_and_finalizes(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    fake, p1, p2, mac_app, seed = dual_env
    confirmed: list[DiskSummary] = []

    def _confirm(s: DiskSummary) -> bool:
        confirmed.append(s)
        return True

    report = burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=_confirm,
    )

    assert fake.provisioned == 1 and fake.finalized == 1
    assert fake.provision_schemes == [config.DUAL_SCHEME_GPT]  # 默认交付级制式
    assert fake.finalize_schemes == [config.DUAL_SCHEME_GPT]
    assert confirmed and confirmed[0].disk_number == 3  # 确认提示拿到了磁盘概要
    assert report.dual_entry
    # 分区1 (可见卷): 双阅览器, 加密链一概不落 —— "盘插上看起来是干净的"
    # (2026-09-23 用户三次定稿: 适配Windows/MAC系统后缀显式名, 旧名全部退役;
    #  2026-10-02 双架构: Mac 入口按芯片一分为二)
    assert (p1 / config.VIEWER_EXE_NAME_DUAL).is_file()
    assert (p1 / config.VIEWER_MAC_DIR_NAME_DUAL_APPLE).is_dir()
    assert (p1 / config.VIEWER_MAC_DIR_NAME_DUAL_INTEL).is_dir()
    assert report.viewer_mac_used is not None and report.viewer_mac_intel_used is not None
    assert not (p1 / config.VIEWER_MAC_DIR_NAME_DUAL_LEGACY).exists()  # 单 Mac 入口名绝不再落盘
    assert not (p1 / config.VIEWER_DIR_NAME).exists()  # 不再出现原名目录
    assert not (p1 / config.VIEWER_WIN_DIR_NAME_FULLWIDTH_LEGACY).exists()  # 旧全角后缀名绝不再落盘
    assert not (p1 / config.VIEWER_WIN_DIR_NAME_DOTTED_LEGACY).exists()  # 旧点前缀名绝不再落盘
    assert not (p1 / config.KEYINFO_NAME).exists()
    assert not (p1 / config.DATA_DIR_NAME).exists()
    # 分区2 (隐藏卷): 加密链全套, 无任何阅览器
    assert (p2 / config.KEYINFO_NAME).is_file()
    assert (p2 / config.MANIFEST_NAME).is_file()
    assert (p2 / config.DATA_DIR_NAME).is_dir()
    assert not (p2 / config.VIEWER_MAC_DIR_NAME).exists()
    assert not (p2 / config.VIEWER_MAC_DIR_NAME_DUAL_APPLE).exists()
    assert not (p2 / config.VIEWER_MAC_DIR_NAME_DUAL_INTEL).exists()
    assert not (p2 / config.VIEWER_DIR_NAME).exists()
    assert any("GPT 无盘符属性" in w for w in report.warnings)
    # 复检与序列号绑定都锚定在加密链所在卷 (分区2)
    vr = inspector.verify_drive(p2, seed, serial_getter=_serial)
    assert vr.all_ok
    assert fake.p1_sizes and fake.p1_sizes[0] > 0  # 分区1按阅览器体积定宽


def test_dual_burn_requires_mac_viewer(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path, tmp_path: Path
) -> None:
    fake, p1, _p2, _app, seed = dual_env
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    opts = BurnOptions(require_root=False, convert_office=False, skip_scan=True, viewer_exe=viewer)
    with pytest.raises(BurnError, match="macOS 查看器"):
        burn_dual(source_dir, p1, seed, opts, serial_getter=_serial)
    assert fake.provisioned == 0


def test_dual_burn_requires_intel_viewer(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path, tmp_path: Path
) -> None:
    """2026-10-02 双架构: 双入口盘缺 Intel 版 .app 直接拒烧 (分区编排之前拦下,
    不烧"只有一半 Mac 收件人能打开"的盘)。"""
    fake, p1, _p2, mac_app, seed = dual_env
    opts = _dual_opts(p1.parent, mac_app, viewer_mac_intel=None)
    with pytest.raises(BurnError, match="Intel 版 macOS 查看器"):
        burn_dual(source_dir, p1, seed, opts, serial_getter=_serial, confirm_wipe=lambda s: True)
    assert fake.provisioned == 0  # 编排前置闸, 不动盘


def test_dual_intel_seed_mismatch_fuses(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path, tmp_path: Path
) -> None:
    """Intel 版 .app 同样吃满三查: 内嵌 SEED 不符 (拿别的机器构建的 Intel 产物
    来烧) 必须熔断 —— 而不是只查 Apple 版放过 Intel 版。"""
    fake, p1, _p2, mac_app, _seed = dual_env
    wrong_intel = _fake_mac_app(tmp_path / "mac_intel_wrong", os.urandom(64))
    opts = _dual_opts(p1.parent, mac_app, viewer_mac_intel=wrong_intel)
    with pytest.raises(BurnError, match="内嵌 SEED 与烧录机 SEED 不一致"):
        burn_dual(source_dir, p1, _seed, opts, serial_getter=_serial, confirm_wipe=lambda s: True)


def test_dual_burn_without_confirm_callback_refuses(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    fake, p1, _p2, mac_app, seed = dual_env
    with pytest.raises(BurnError, match="确认"):
        burn_dual(
            source_dir, p1, seed, _dual_opts(p1.parent, mac_app), serial_getter=_serial
        )  # confirm_wipe=None: 脚本误操作的保险丝
    assert fake.provisioned == 0


def test_dual_burn_confirm_declined_aborts(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    fake, p1, _p2, mac_app, seed = dual_env
    (p1 / "用户文件.txt").write_text("不该被动到", encoding="utf-8")
    with pytest.raises(BurnError, match="已取消"):
        burn_dual(
            source_dir,
            p1,
            seed,
            _dual_opts(p1.parent, mac_app),
            serial_getter=_serial,
            confirm_wipe=lambda s: False,
        )
    assert fake.provisioned == 0
    assert (p1 / "用户文件.txt").read_text(encoding="utf-8") == "不该被动到"


def test_dual_reburn_reuses_layout_no_clean_no_prompt(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path, tmp_path: Path
) -> None:
    fake, p1, p2, mac_app, seed = dual_env
    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(tmp_path, mac_app),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    assert fake._scheme == config.DUAL_SCHEME_GPT

    def no_prompt(_s: DiskSummary) -> bool:
        raise AssertionError("重烧复用布局不应再触发清盘确认")

    mac_app2 = _fake_mac_app(tmp_path / "mac_dist2", seed, macho=b"MACHO-V2")
    report2 = burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(tmp_path, mac_app2),
        serial_getter=_serial,
        confirm_wipe=no_prompt,
    )
    assert fake.provisioned == 1 and fake.refreshed == 1 and fake.finalized == 2
    assert any("复用分区" in w for w in report2.warnings)
    # v2.4.0 反转布局: .app 刷新在分区1; 分区2 加密链原位重写 (keyinfo 仍在)
    macho = p1 / config.VIEWER_MAC_DIR_NAME_DUAL_APPLE / "Contents" / "MacOS" / "图书阅览器"
    assert macho.read_bytes() == b"MACHO-V2"
    assert (p2 / config.KEYINFO_NAME).is_file()
    assert inspector.verify_drive(p2, seed, serial_getter=_serial).all_ok


def test_dual_reburn_exfat_generation_gate_rebuilds(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-28 换格式=换代: 任一分区非 NTFS (exFAT 旧形态) → 不走复用,
    清盘重建为 NTFS 交付形态 (同"换名=换代"先例 —— 旧形态没有防误删 ACL,
    不能借复用路径一直留存)。"""
    fake, p1, p2, mac_app, seed = dual_env
    burn_dual(
        source_dir, p1, seed, _dual_opts(tmp_path, mac_app),
        serial_getter=_serial, confirm_wipe=lambda s: True,
    )
    assert fake.provisioned == 1
    monkeypatch.setattr(burner, "_volume_fs", lambda root: "exFAT")  # 旧形态盘
    confirmed: list[DiskSummary] = []

    def _confirm(s: DiskSummary) -> bool:
        confirmed.append(s)
        return True

    report2 = burn_dual(
        source_dir, p1, seed, _dual_opts(tmp_path, mac_app),
        serial_getter=_serial, confirm_wipe=_confirm,
    )
    assert fake.provisioned == 2 and fake.refreshed == 1  # 挂出看过内容, 但判换代
    assert confirmed  # 重建前过了人工确认
    assert any("换格式=换代" in w for w in report2.warnings)
    assert not any("复用分区" in w for w in report2.warnings)
    assert (p2 / config.KEYINFO_NAME).is_file()  # 重建后链照常在场


def test_volume_fs_calls_imported_get_volume_fs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-29 mypy 抓出的真 bug 回归钉: _volume_fs 曾引用未导入的
    volume_id 模块名, NameError 被 except Exception 吞成 "" —— 真机上
    双分区复用判据恒判"非 NTFS"误走全盘重建 (测试注入 _volume_fs 掩盖了
    此路)。钉住: 包装必须经模块级已导入的 get_volume_fs 真调用。"""
    seen: list[Path] = []

    def fake_get(root: Path) -> str:
        seen.append(root)
        return "NTFS"

    monkeypatch.setattr(burner, "get_volume_fs", fake_get)
    assert burner._volume_fs(Path("X:/")) == "NTFS"
    assert seen == [Path("X:/")]


def test_dual_reuse_is_content_based_not_signature_based(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """旧版布局 (分区2=.app, 表签名与反转布局完全相同): 必须按内容判别走
    清盘重建 + 确认, 绝不能误当反转布局复用。"""
    fake, p1, p2, mac_app, seed = dual_env
    fake._scheme = config.DUAL_SCHEME_GPT  # 旧版盘同样是无盘符 GPT 双分区
    old_app = p2 / config.VIEWER_MAC_DIR_NAME  # 内容却是 .app 而非加密链
    (old_app / "Contents" / "MacOS").mkdir(parents=True)
    (old_app / "Contents" / "MacOS" / "图书阅览器").write_bytes(b"OLD-LAYOUT")
    confirmed: list[DiskSummary] = []

    def _confirm(s: DiskSummary) -> bool:
        confirmed.append(s)
        return True

    report = burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=_confirm,
    )
    assert fake.refreshed == 1  # 挂出来看过内容了
    assert fake.provisioned == 1  # 无 keyinfo → 全盘重建
    assert confirmed  # 重建过了人工确认门
    assert any("全盘重建" in w for w in report.warnings)
    assert (p2 / config.KEYINFO_NAME).is_file()  # 重建后加密链就位
    assert not old_app.exists()  # 旧 .app 随 clean 消失


def test_dual_reuse_rebuilds_when_p1_below_target(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复用判据之二 (2026-09-22 用户定稿: 分区1底线 1 GiB): 既有反转布局
    (分区2 有 keyinfo) 但分区1尺寸低于本轮应给 —— 旧 542 MiB 盘重烧必须
    全盘重建扩容 (过确认), 不能借复用路径静默保留小分区1。"""
    fake, p1, p2, mac_app, seed = dual_env
    fake._scheme = config.DUAL_SCHEME_GPT
    (p2 / config.KEYINFO_NAME).write_bytes(b"chain")
    real_usage = shutil.disk_usage
    # 分区1卷容量伪装成 128 MiB (fake 的 viewer_part_size_mib 给 256 MiB)
    small = real_usage(p1)._replace(total=128 * 1024 * 1024)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda p: small if Path(p) == p1 else real_usage(p)
    )
    confirmed: list[DiskSummary] = []

    def _confirm(s: DiskSummary) -> bool:
        confirmed.append(s)
        return True

    report = burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=_confirm,
    )
    assert fake.refreshed == 1  # 挂出来看过内容了 (keyinfo 在)
    assert fake.provisioned == 1  # 分区1过小 → 重建扩容而非复用
    assert confirmed  # 重建过了人工确认门
    assert any("全盘重建扩容" in w for w in report.warnings)
    assert not any("复用分区" in w for w in report.warnings)
    assert (p2 / config.KEYINFO_NAME).is_file()  # 重建后新加密链就位


def test_dual_reuse_rebuilds_when_p1_raw(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分区1文件系统读不出 (RAW; 2026-09-22 真机白牌棒拔插即坏形态): 复用
    无从谈起, 容量查询失败同样按不达标走清盘重建。"""
    fake, p1, p2, mac_app, seed = dual_env
    fake._scheme = config.DUAL_SCHEME_GPT
    (p2 / config.KEYINFO_NAME).write_bytes(b"chain")
    real_usage = shutil.disk_usage

    def _raw_on_p1(p: Any) -> Any:
        if Path(p) == p1:
            raise OSError("设备未就绪 (RAW 卷)")
        return real_usage(p)

    monkeypatch.setattr(shutil, "disk_usage", _raw_on_p1)

    report = burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    assert fake.provisioned == 1  # RAW → 重建
    assert any("分区1容量读不出" in w for w in report.warnings)
    assert not any("复用分区" in w for w in report.warnings)


def test_dual_mbr_scheme_passed_through(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """--dual-scheme mbr (快速验证制式): provision/finalize 全链带 mbr。"""
    fake, p1, _p2, mac_app, seed = dual_env
    report = burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app, dual_scheme=config.DUAL_SCHEME_MBR),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    assert fake.provision_schemes == [config.DUAL_SCHEME_MBR]
    assert fake.finalize_schemes == [config.DUAL_SCHEME_MBR]
    assert any("MBR 快速验证制式" in w for w in report.warnings)


def test_dual_unknown_scheme_refuses(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    fake, p1, _p2, mac_app, seed = dual_env
    with pytest.raises(BurnError, match="未知双入口制式"):
        burn_dual(
            source_dir,
            p1,
            seed,
            _dual_opts(p1.parent, mac_app, dual_scheme="bogus"),
            serial_getter=_serial,
            confirm_wipe=lambda s: True,
        )
    assert fake.provisioned == 0


def test_dual_legacy_0x17_rebuilt_after_content_check(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """旧 0x17 测试盘 (分区2=.app 的旧版布局): mbr 重烧时先迁 0x07 挂出看
    内容, 无 keyinfo → 全盘重建 (过确认)。旧版"仅迁移不清盘"的语义随
    反转布局作废 —— 旧布局分区2装不下加密链, 必须重建。"""
    fake, p1, p2, mac_app, seed = dual_env
    fake._scheme = config.DUAL_SCHEME_MBR
    fake._legacy_0x17 = True
    old_app = p2 / config.VIEWER_MAC_DIR_NAME
    (old_app / "Contents" / "MacOS").mkdir(parents=True)
    (old_app / "Contents" / "MacOS" / "图书阅览器").write_bytes(b"OLD-0x17")

    report = burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app, dual_scheme=config.DUAL_SCHEME_MBR),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    assert fake.refreshed == 1 and fake.refresh_migrations == [True]  # 迁移后看内容
    assert fake.provisioned == 1  # 旧内容 → 重建
    # 重建路径上迁移只是内容判别的前置动作 (随后 clean), 提示只报全盘重建
    assert any("全盘重建" in w for w in report.warnings)
    assert (p2 / config.KEYINFO_NAME).is_file()


def test_dual_scheme_mismatch_reprovisions_with_confirm(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """盘上是 gpt 布局却要 mbr (或反之): 不复用, 走清盘重建 + 确认。"""
    fake, p1, p2, mac_app, seed = dual_env
    fake._scheme = config.DUAL_SCHEME_GPT
    (p2 / config.KEYINFO_NAME).write_bytes(b"chain")  # 内容像反转布局也不行
    confirmed: list[DiskSummary] = []

    def _confirm(s: DiskSummary) -> bool:
        confirmed.append(s)
        return True

    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app, dual_scheme=config.DUAL_SCHEME_MBR),
        serial_getter=_serial,
        confirm_wipe=_confirm,
    )
    assert fake.refreshed == 0 and fake.provisioned == 1
    assert fake.provision_schemes == [config.DUAL_SCHEME_MBR]
    assert confirmed and confirmed[0].disk_number == 3


def test_dual_finalize_runs_even_when_burn_fails(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, p1, _p2, mac_app, seed = dual_env

    def boom(*a: object, **kw: object) -> None:
        raise inspector.InspectorError("复检通道炸了")

    monkeypatch.setattr(inspector, "verify_drive", boom)
    with pytest.raises(inspector.InspectorError):
        burn_dual(
            source_dir,
            p1,
            seed,
            _dual_opts(p1.parent, mac_app),
            serial_getter=_serial,
            confirm_wipe=lambda s: True,
        )
    assert fake.finalized == 1  # 失败也归还临时盘符


# ---------------------------------------------------------------------------
# 清理兼容 / 空间 / 回归闸
# ---------------------------------------------------------------------------


def test_dual_reburn_clears_stale_artifacts_both_partitions(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """复用重烧: 分区1的旧阅览器 (原名 + 旧点前缀名 + 旧 .app) 与分区2的
    旧版布局残留 .app 都要清掉, 加密链原位重写。"""
    fake, p1, p2, mac_app, seed = dual_env
    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    # 制造陈旧产物: 分区1 原名目录 (旧单分区盘升级) + 分区1 旧点前缀目录
    # (2026-09-22 及之前的双入口名) + 分区1 旧全角（WIN）后缀目录 (三代名,
    # 2026-09-23 适配后缀改名的前一代) + 分区2 旧 .app (旧双分区布局残留,
    # 复用重烧时 _clear_own_artifacts 负责联合清扫)
    old_plain = p1 / config.VIEWER_DIR_NAME
    old_plain.mkdir()
    (old_plain / "旧.exe").write_bytes(b"OLD")
    os.chmod(old_plain / "旧.exe", stat.S_IREAD)  # 只读也必须清得掉
    old_dotted = p1 / config.VIEWER_WIN_DIR_NAME_DOTTED_LEGACY
    (old_dotted / "_internal").mkdir(parents=True)
    (old_dotted / "_internal" / "旧.dll").write_bytes(b"OLD-DOTTED")
    old_fw = p1 / config.VIEWER_WIN_DIR_NAME_FULLWIDTH_LEGACY
    (old_fw / "_internal").mkdir(parents=True)
    (old_fw / "_internal" / "旧.dll").write_bytes(b"OLD-FULLWIDTH")
    old_p2_app = p2 / config.VIEWER_MAC_DIR_NAME
    (old_p2_app / "Contents" / "MacOS").mkdir(parents=True)
    (old_p2_app / "Contents" / "MacOS" / "图书阅览器").write_bytes(b"STALE")

    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    assert not old_plain.exists()  # 分区1 原名目录清掉
    assert not old_dotted.exists()  # 分区1 旧点前缀目录清掉 (legacy 清扫名单)
    assert not old_fw.exists()  # 分区1 旧全角后缀目录清掉 (三代名清扫)
    assert not old_p2_app.exists()  # 分区2 旧 .app 清掉
    assert (p1 / config.VIEWER_EXE_NAME_DUAL).is_file()  # 新阅览器就位
    assert (p2 / config.KEYINFO_NAME).is_file()  # 加密链原位
    assert inspector.verify_drive(p2, seed, serial_getter=_serial).all_ok


def test_dual_p1_space_check_aborts_on_reuse(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复用重烧时分区1装不下新阅览器: 明确报错并指出恢复路径。"""
    fake, p1, p2, mac_app, seed = dual_env
    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )

    real_usage = shutil.disk_usage

    def fake_usage(arg: str | Path, *a: object, **kw: object) -> Any:
        if Path(arg) == p1:
            # 分区1只剩 1KB (总量给足: 本用例测空间预检, 不测尺寸底线)
            return SimpleNamespace(total=1024 * 1024 * 1024, free=1024)
        return real_usage(arg)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    with pytest.raises(BurnError, match="分区1空间不足"):
        burn_dual(
            source_dir,
            p1,
            seed,
            _dual_opts(p1.parent, mac_app),
            serial_getter=_serial,
            confirm_wipe=lambda s: True,
        )


def test_dual_chain_space_check_aborts(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加密链所在卷 (分区2) 装不下源资料: burn() 的空间预检拦下。"""
    fake, p1, p2, mac_app, seed = dual_env
    real_usage = shutil.disk_usage

    def fake_usage(arg: str | Path, *a: object, **kw: object) -> Any:
        if Path(arg) == p2:
            return SimpleNamespace(free=1024)  # 分区2只剩 1KB
        return real_usage(arg)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    with pytest.raises(BurnError, match="空间可能不足"):
        burn_dual(
            source_dir,
            p1,
            seed,
            _dual_opts(p1.parent, mac_app),
            serial_getter=_serial,
            confirm_wipe=lambda s: True,
        )


def test_dual_burn_non_windows_refuses(
    source_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    seed = os.urandom(64)
    mac_app = _fake_mac_app(tmp_path, seed)
    with pytest.raises(BurnError, match="仅.*Windows"):
        burn_dual(
            source_dir,
            tmp_path / "P1",
            seed,
            _dual_opts(tmp_path, mac_app),
            serial_getter=_serial,
            confirm_wipe=lambda s: True,
        )


def test_single_mode_never_touches_disk_layout(
    source_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单分区默认流的回归闸: 不触碰 disk_layout, 落盘名保持原样。"""

    def boom() -> None:
        raise AssertionError("单分区流不应触碰 disk_layout")

    monkeypatch.setattr(disk_layout_mod, "require_admin", boom)
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    drive = tmp_path / "USB"
    drive.mkdir()
    report = burn(
        source_dir,
        drive,
        b"S" * 64,
        BurnOptions(require_root=False, viewer_exe=viewer, convert_office=False, skip_scan=True),
        serial_getter=_serial,
    )
    assert not report.dual_entry
    assert (drive / config.VIEWER_EXE_NAME).is_file()  # 原名, 无点前缀
    assert (drive / config.KEYINFO_NAME).is_file()  # 单分区: 链就在本卷


# ---------------------------------------------------------------------------
# lock_drive / 隐藏属性 (win32 真实断言)
# ---------------------------------------------------------------------------


def test_lock_drive_locks_chain_partition(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """反转布局的补锁对象是加密链所在卷 (分区2) —— 分区1 上没有 keyinfo。"""
    fake, p1, p2, mac_app, seed = dual_env
    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    _make_writable(p2)  # 解开烧录时上的只读, lock 再统一补
    warnings, n = lock_drive(p2)
    keyinfo = p2 / config.KEYINFO_NAME
    assert keyinfo.is_file()
    assert not keyinfo.stat().st_mode & stat.S_IWUSR  # 密文载体补回只读


@pytest.mark.skipif(sys.platform != "win32", reason="win32 文件属性真实断言")
def test_dual_hide_chain_on_p2_win_internal_on_p1(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
    source_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    from src.app.usb_vault.volume_id import set_hidden as real_set_hidden

    monkeypatch.setattr(burner, "set_hidden", real_set_hidden)
    fake, p1, p2, mac_app, seed = dual_env
    # onedir Windows 查看器: _internal 依赖树 + 入口 exe
    onedir = tmp_path / "win_dist" / config.VIEWER_DIR_NAME
    internal = onedir / "_internal"
    internal.mkdir(parents=True)
    (internal / "seed.bin").write_bytes(seed)
    (onedir / config.VIEWER_EXE_NAME).write_bytes(b"FAKE-EXE")

    opts = BurnOptions(
        require_root=False,
        convert_office=False,
        skip_scan=True,
        viewer_exe=onedir,
        viewer_mac=mac_app,
        viewer_mac_intel=_fake_mac_app(tmp_path / "mac_intel", seed),
        launcher_exe=tmp_path / "launcher.exe",
    )
    (tmp_path / "launcher.exe").write_bytes(b"LAUNCHER-SHELL")
    burn_dual(source_dir, p1, seed, opts, serial_getter=_serial, confirm_wipe=lambda s: True)

    def attrs(p: Path) -> int:
        return int(ctypes.windll.kernel32.GetFileAttributesW(str(p)))

    # 分区2: 加密链全套 +H+S (卷本身已无盘符, 双保险)
    assert attrs(p2 / config.KEYINFO_NAME) & 0x6 == 0x6
    assert attrs(p2 / config.MANIFEST_NAME) & 0x6 == 0x6
    assert attrs(p2 / config.DATA_DIR_NAME) & 0x6 == 0x6
    # 分区1: onedir 依赖树 H+S, 入口 exe 保持可见
    win_dir = p1 / config.VIEWER_WIN_DIR_NAME_DUAL
    assert win_dir.is_dir()
    assert attrs(win_dir / "_internal") & 0x6 == 0x6  # 依赖树目录 H+S (整树不可见)
    assert attrs(win_dir / "_internal" / "seed.bin") & 0x1 == 0x1  # 内部文件只读
    # 双入口目录内的可执行文件 (v2.4.5): 书名 exe = 自包含引导壳 (可见,
    # 单拷走弹谢幕框), 真主程序保持构建名 VIEWER_EXE_NAME (引导壳按名转交);
    # v2.4.6: 真主程序 +H+S 隐藏 —— 顶着一代旧名裸露会被当旧版残留清掉
    # (2026-09-23 真棒事故), 藏到与 _internal 同待遇, 目录里唯一可见入口=书名 exe
    exe = win_dir / config.VIEWER_INNER_EXE_NAME_DUAL
    assert exe.is_file()
    assert (win_dir / config.VIEWER_EXE_NAME).is_file()  # 真主程序不再被改名
    assert not attrs(exe) & 0x2  # 书名引导壳保持可见
    assert attrs(win_dir / config.VIEWER_EXE_NAME) & 0x6 == 0x6  # 真主程序隐藏+系统


@pytest.mark.skipif(sys.platform != "win32", reason="win32 只读位真实断言")
def test_dual_viewer_entries_readonly_set(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """2026-09-20 防误删评审收口: 双入口的阅读器入口 (分区1 落盘名
    exe + 分区1 .app 整树) 与加密链载体全文件 +R。

    **防删声明已撤回 (同日真机+探针双证)**: 资源管理器删除会先剥只读属性
    再删 (单文件/整目录都删得掉); 只读位挡的是改写/覆写与不带强制参数的
    命令行删除, 误删恢复靠备份或重烧。见 test_burner 同名用例的完整声明。
    """
    import ctypes

    fake, p1, p2, mac_app, seed = dual_env
    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )

    def attrs(p: Path) -> int:
        return int(ctypes.windll.kernel32.GetFileAttributesW(str(p)))

    def readonly(p: Path) -> None:
        assert p.exists(), f"产物缺失: {p}"
        a = attrs(p)
        assert a & 0x1 == 0x1, f"缺只读位: {p.name} -> {a:#x}"

    readonly(p1 / config.VIEWER_EXE_NAME_DUAL)  # 分区1 Windows 入口
    app = p1 / config.VIEWER_MAC_DIR_NAME_DUAL_APPLE  # 分区1 macOS 入口 (v2.4.0 同卷)
    readonly(app / "Contents" / "MacOS" / "图书阅览器")
    readonly(app / "Contents" / "Info.plist")
    readonly(app / "Contents" / "Frameworks" / "seed.bin")
    readonly(p2 / config.KEYINFO_NAME)  # 加密链载体
    readonly(p2 / config.MANIFEST_NAME)

    # DeleteFileW 直调拒绝只读文件 (改写类防护契约锚点, 非 Explorer 路径)
    with pytest.raises(PermissionError):
        os.remove(p1 / config.VIEWER_EXE_NAME_DUAL)
    with pytest.raises(PermissionError):
        os.remove(app / "Contents" / "MacOS" / "图书阅览器")
    with pytest.raises(PermissionError):
        os.remove(p2 / config.KEYINFO_NAME)


def test_dual_always_labels_p1_with_title(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """2026-09-23 真机: 未设卷标的 exFAT 分区1在 Mac Finder 显示 Untitled
    (Windows 显示 新加卷), 而收件人手册承诺桌面出现 图书资料 卷名。
    双入口烧录分区1卷标恒落 title, 不再依赖 --set-label 开关; --title 图书资料
    的交付流程自然得到正名, 测试烧录 (默认 title=资料) 也不再出 Untitled 盘。
    """
    fake, p1, p2, mac_app, seed = dual_env
    report = burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),  # 未带 set_label / title
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    labels = {str(root): label for root, label in fake.volume_labels}
    assert labels[str(p1)] == "资料"  # = BurnOptions.title 默认值
    assert labels[str(p2)] == config.PART2_LABEL  # .资料存储 (v2.4.7 Mac 隐藏)
    # 卷标落成 → 不应再有观感告警 (分区2 GPT 说明等常规告警照常存在)
    assert not [w for w in report.warnings if "分区1卷标" in w]


def test_dual_p2_label_failure_fails_closed(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
    source_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2.4.7: 分区2卷标 (.资料存储) 即 Mac 侧藏匿机制 —— 设置失败不再是
    "仅告警"观感项: 收尾 (盘符隐藏) 走完后熔断, 重跑自愈 (复用路径原位重写)。"""

    def _flaky_label(root: Path, label: str) -> bool:
        return label != config.PART2_LABEL  # 只让分区2卷标失败

    fake, p1, p2, mac_app, seed = dual_env
    monkeypatch.setattr(burner, "set_volume_label", _flaky_label)
    with pytest.raises(BurnError, match="分区2卷标未能设置"):
        burn_dual(
            source_dir,
            p1,
            seed,
            _dual_opts(p1.parent, mac_app),
            serial_getter=_serial,
            confirm_wipe=lambda s: True,
        )
    assert fake.finalized >= 1  # 熔断前分区2盘符隐藏收尾已走完, 不残留挂载


def test_dual_legacy_plain_name_chain_rebuilds(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path
) -> None:
    """v2.4.7 换名=换代: 旧明文名密文链 (v2.4.0~2.4.6 盘) 不再算"本工具
    加密链" → 全盘重建; 新链落点前缀名, 旧名文件被清扫不留残链。"""
    fake, p1, p2, mac_app, seed = dual_env
    (p2 / config.KEYINFO_NAME_LEGACY).write_bytes(b"old-chain")
    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )
    assert fake.provisioned == 1  # 未走复用 (复用判据只认新名)
    assert (p2 / config.KEYINFO_NAME).is_file()
    assert not (p2 / config.KEYINFO_NAME_LEGACY).exists()  # 清扫没残留


def test_chain_names_dot_prefixed_for_mac_hiding() -> None:
    """v2.4.7: 密文链名与分区2卷标全部点前缀 —— macOS 不认 DOS 隐藏位
    (+H+S 只在 Windows 生效), 点前缀是 Mac 侧唯一的观感隐藏 (Finder/ls
    默认不显示点前缀挂载点与文件)。改名须整链同步: 烧录/阅览器/检视共用
    这组常量, 换名=换代 (旧名盘重烧走全盘重建)。"""
    assert config.KEYINFO_NAME == ".keyinfo.bin"
    assert config.MANIFEST_NAME == ".manifest.enc"
    assert config.DATA_DIR_NAME == ".DATA"
    assert config.PART2_LABEL == ".资料存储"
    for new, legacy in (
        (config.KEYINFO_NAME, config.KEYINFO_NAME_LEGACY),
        (config.MANIFEST_NAME, config.MANIFEST_NAME_LEGACY),
        (config.DATA_DIR_NAME, config.DATA_DIR_NAME_LEGACY),
    ):
        assert new.startswith(".") and not legacy.startswith(".")


def test_dual_onedir_book_exe_is_selfcontained_launcher(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path, tmp_path: Path
) -> None:
    """v2.4.5: 双入口目录版收件人双击的书名 exe = 自包含引导壳 (不再是从
    构建名改名来的真主程序)。旧方案 (v2.4.4 及之前) 把 onedir 真主程序改名
    成书名, 被单独拷走后死在 PyInstaller C 装载器层, 弹英文 DLL 错误而非
    中文谢幕文案 (2026-09-23 用户复测曝光)。新布局: 书名=引导壳 (字节与
    构建产物一致), 真主程序保持构建名 VIEWER_EXE_NAME (壳按名转交), 依赖
    树 (_internal) 原样不动。
    """
    fake, p1, p2, mac_app, seed = dual_env
    onedir = tmp_path / "win_onedir"
    (onedir / "_internal").mkdir(parents=True)
    (onedir / config.VIEWER_EXE_NAME).write_bytes(b"EXE-BYTES")
    (onedir / "_internal" / "seed.bin").write_bytes(seed)
    (onedir / "_internal" / "lib.dll").write_bytes(b"LIB")

    burn_dual(
        source_dir,
        p1,
        seed,
        _dual_opts(p1.parent, mac_app, viewer_exe=onedir),
        serial_getter=_serial,
        confirm_wipe=lambda s: True,
    )

    win_dir = p1 / config.VIEWER_WIN_DIR_NAME_DUAL
    book_exe = win_dir / config.VIEWER_INNER_EXE_NAME_DUAL
    assert book_exe.is_file()
    assert book_exe.read_bytes() == b"LAUNCHER-SHELL"  # 书名=引导壳本体 (非真主程序)
    assert (win_dir / config.VIEWER_EXE_NAME).is_file()  # 真主程序保持构建名
    assert (win_dir / config.VIEWER_EXE_NAME).read_bytes() == b"EXE-BYTES"
    assert (win_dir / "_internal" / "seed.bin").is_file()  # 依赖树原样
    assert (win_dir / "_internal" / "lib.dll").is_file()


def test_dual_onedir_missing_launcher_fails_closed(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes],
    source_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2.4.5 熔断: 双入口目录版没有书名引导壳 = 收件人双击书名 exe 会弹
    英文 DLL 错误 (本任务要根治的缺陷), 绝不让这种盘出炉。Mac 烧录机无法
    自动构建 → 必须给出人话指引 (--launcher-exe)。
    """
    fake, p1, p2, mac_app, seed = dual_env
    onedir = tmp_path / "win_onedir"
    (onedir / "_internal").mkdir(parents=True)
    (onedir / config.VIEWER_EXE_NAME).write_bytes(b"EXE-BYTES")
    (onedir / "_internal" / "seed.bin").write_bytes(seed)
    # 模拟"解析不到引导壳"的结局 (Mac 烧录机 dist 无产物 + 无法自动构建);
    # 不直接改 sys.platform —— 双入口另有"须 Windows 烧录机"平台闸会先拦
    monkeypatch.setattr(burner, "_resolve_dual_launcher", lambda opts, warnings: None)

    with pytest.raises(burner.BurnError, match="书名引导壳"):
        burn_dual(
            source_dir,
            p1,
            seed,
            _dual_opts(p1.parent, mac_app, viewer_exe=onedir, launcher_exe=None),
            serial_getter=_serial,
            confirm_wipe=lambda s: True,
        )


def test_dual_onedir_real_exe_missing_fails_closed(
    dual_env: tuple[_FakeDisk, Path, Path, Path, bytes], source_dir: Path, tmp_path: Path
) -> None:
    """v2.4.5 熔断: 目录里没有构建名真主程序 (VIEWER_EXE_NAME) 时, 引导壳
    无从转交 —— 盘上双击书名只会弹谢幕框。旧方案这里只告警跳过改名, 现在
    转交链是 load-bearing 契约, 缺失即断烧。
    """
    fake, p1, p2, mac_app, seed = dual_env
    onedir = tmp_path / "win_onedir"
    (onedir / "_internal").mkdir(parents=True)
    # 主程序不叫构建名 (自定义 --name 产物), SEED 链完整
    (onedir / "自定义名.exe").write_bytes(b"EXE-BYTES")
    (onedir / "_internal" / "seed.bin").write_bytes(seed)

    with pytest.raises(burner.BurnError, match="无从转交"):
        burn_dual(
            source_dir,
            p1,
            seed,
            _dual_opts(p1.parent, mac_app, viewer_exe=onedir),
            serial_getter=_serial,
            confirm_wipe=lambda s: True,
        )
