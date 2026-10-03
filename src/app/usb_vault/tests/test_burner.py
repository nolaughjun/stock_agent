"""burner 端到端: tmp 目录当"U盘", 注入序列号, 不碰真实硬件。

关键用例: 克隆仿真 —— 同一份盘内容换序列号后必须解不开
(整盘拷贝到其他U盘/硬盘的核心拦截点)。
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from src.app.usb_vault import config, inspector, unlock_code
from src.app.usb_vault.burner import BurnError, BurnOptions, burn, extended_path

# 隔离开发机真实 dist/ (conftest.dist_isolated), 全文件生效
pytestmark = pytest.mark.usefixtures("dist_isolated")

SERIAL = 0x11223344
OTHER_SERIAL = 0xAABBCCDD


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "资料"
    (src / "合同").mkdir(parents=True)
    (src / "说明.txt").write_text("机密说明内容", encoding="utf-8")
    (src / "图.png").write_bytes(b"\x89PNG fake image bytes")
    (src / "报价.pdf").write_bytes(b"%PDF-1.4 fake")
    (src / "data.zip").write_bytes(b"PK fake zip")
    (src / "合同" / "合同正文.docx").write_bytes(b"PK fake docx")
    return src


@pytest.fixture()
def drive_dir(tmp_path: Path) -> Path:
    return tmp_path / "USB"


@pytest.fixture()
def fake_viewer(tmp_path: Path) -> Path:
    p = tmp_path / "viewer.exe"
    p.write_bytes(b"FAKE-EXE")
    return p


def _opts(**kw: object) -> BurnOptions:
    # skip_scan=True: 单测不依赖真实杀毒引擎 (AV 门有专门用例显式打开)
    base: dict[str, object] = dict(require_root=False, convert_office=False, skip_scan=True)
    base.update(kw)
    return BurnOptions(**base)  # type: ignore[arg-type]


def _serial(value: int) -> Callable[[Path], int]:
    return lambda _p: value


def _make_writable_tree(root: Path) -> None:
    if sys.platform == "win32" and root.is_dir():
        # 2026-09-28 起烧录收尾施加 NTFS 拒删 ACL (tmp 在 C: = 真 NTFS,
        # icacls 真跑): 清理前先整树摘掉, 否则 tmp 目录残留拒删 ACE
        from src.app.usb_vault import ntfs_guard

        if ntfs_guard.is_ntfs(root):
            try:
                ntfs_guard.strip_delete_denial(root)
            except ntfs_guard.NtfsGuardError:
                pass
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            try:
                os.chmod(os.path.join(dirpath, n), stat.S_IWUSR | stat.S_IRUSR)
            except OSError:
                pass


@pytest.fixture(autouse=True)
def _cleanup_readonly(drive_dir: Path) -> Iterator[None]:
    yield
    if drive_dir.exists():
        _make_writable_tree(drive_dir)


_SEED: dict[str, bytes] = {}


def _seed_of_burn() -> bytes:
    return _SEED["seed"]


@pytest.fixture()
def burned(source_dir: Path, drive_dir: Path, fake_viewer: Path,
           dist_isolated: None) -> Path:
    seed = os.urandom(64)
    _SEED["seed"] = seed
    burn(source_dir, drive_dir, seed, _opts(viewer_exe=fake_viewer),
         serial_getter=_serial(SERIAL))
    return drive_dir


# ------------------------------------------------------------------ 端到端

def test_burn_products_and_verify(burned: Path) -> None:
    assert (burned / config.KEYINFO_NAME).is_file()
    assert (burned / config.MANIFEST_NAME).is_file()
    assert (burned / "图书阅览器.exe").is_file()
    enc_files = list((burned / config.DATA_DIR_NAME).glob("*.enc"))
    assert len(enc_files) == 5  # 5 个源文件


def test_verify_all_ok(burned: Path) -> None:
    report = inspector.verify_drive(burned, _seed_of_burn(), serial_getter=_serial(SERIAL))
    assert report.all_ok
    assert len(report.results) == 5


def test_media_classification(burned: Path) -> None:
    m, _ = inspector.open_manifest(burned, _seed_of_burn(), serial_getter=_serial(SERIAL))
    by_rel = {f.rel_path: f for f in m.files}
    assert by_rel["说明.txt"].media == "text"
    assert by_rel["图.png"].media == "image"
    assert by_rel["报价.pdf"].media == "pdf"
    assert by_rel["data.zip"].media == "binary"
    # --no-convert: office 记为 binary
    assert by_rel["合同/合同正文.docx"].media == "binary"
    assert by_rel["合同/合同正文.docx"].converted_from is None


def test_files_are_readonly(burned: Path) -> None:
    enc = next((burned / config.DATA_DIR_NAME).glob("*.enc"))
    assert not os.access(enc, os.W_OK)


def test_reburn_clears_old(burned: Path, source_dir: Path, fake_viewer: Path) -> None:
    seed = _seed_of_burn()
    report = burn(source_dir, burned, seed, _opts(viewer_exe=fake_viewer),
                  serial_getter=_serial(SERIAL))
    assert report.file_count == 5
    assert len(list((burned / config.DATA_DIR_NAME).glob("*.enc"))) == 5  # 旧的没残留


def test_reburn_clears_legacy_plain_chain(
    burned: Path, source_dir: Path, fake_viewer: Path
) -> None:
    """v2.4.7 点前缀换代: 旧明文名密文链 (v2.4.0~2.4.6 盘) 重烧时被清扫,
    不留残链 —— 旧 keyinfo 是别的 SEED 的盐, 留着只会污染 Mac 观感与排障。"""
    burned.joinpath(config.KEYINFO_NAME_LEGACY).write_bytes(b"old")
    burned.joinpath(config.MANIFEST_NAME_LEGACY).write_bytes(b"old")
    legacy_data = burned / config.DATA_DIR_NAME_LEGACY
    legacy_data.mkdir()
    (legacy_data / "deadbeef.enc").write_bytes(b"old")
    burn(source_dir, burned, _seed_of_burn(), _opts(viewer_exe=fake_viewer),
         serial_getter=_serial(SERIAL))
    assert (burned / config.KEYINFO_NAME).is_file()  # 新名链就位
    assert not (burned / config.KEYINFO_NAME_LEGACY).exists()
    assert not (burned / config.MANIFEST_NAME_LEGACY).exists()
    assert not legacy_data.exists()


# ------------------------------------------------------------------ lock (防拷贝加固)


def test_lock_restores_readonly_after_unlock(burned: Path) -> None:
    """收件人清掉只读属性后, lock 免重烧恢复 +R; 幂等可重复执行。"""
    from src.app.usb_vault.burner import lock_drive

    enc = next((burned / config.DATA_DIR_NAME).glob("*.enc"))
    os.chmod(enc, stat.S_IRUSR | stat.S_IWUSR)  # 模拟属性被解开
    assert os.access(enc, os.W_OK)
    warnings, count = lock_drive(burned)
    assert count == 8  # 5 密文 + keyinfo + manifest + 查看器 exe
    assert not os.access(enc, os.W_OK)
    assert not os.access(burned / config.KEYINFO_NAME, os.W_OK)
    assert not os.access(burned / "图书阅览器.exe", os.W_OK)
    warnings2, count2 = lock_drive(burned)  # 幂等
    assert count2 == count


def test_lock_non_vault_drive_rejected(tmp_path: Path) -> None:
    from src.app.usb_vault.burner import lock_drive

    empty = tmp_path / "空盘"
    empty.mkdir()
    with pytest.raises(BurnError, match="不是资料盘"):
        lock_drive(empty)


# ------------------------------------------------------------------ 克隆仿真

def test_clone_other_serial_fails(burned: Path) -> None:
    with pytest.raises(inspector.WrongDriveError):
        inspector.open_manifest(burned, _seed_of_burn(), serial_getter=_serial(OTHER_SERIAL))


def test_clone_copytree_other_drive_fails(burned: Path, tmp_path: Path) -> None:
    clone = tmp_path / "CLONE"
    shutil.copytree(burned, clone)
    with pytest.raises(inspector.WrongDriveError):
        inspector.open_manifest(clone, _seed_of_burn(), serial_getter=_serial(OTHER_SERIAL))


def test_original_still_opens_after_clone_attempts(burned: Path) -> None:
    m, _ = inspector.open_manifest(burned, _seed_of_burn(), serial_getter=_serial(SERIAL))
    assert len(m.files) == 5


def test_wrong_seed_fails(burned: Path) -> None:
    with pytest.raises(inspector.WrongDriveError):
        inspector.open_manifest(burned, os.urandom(64), serial_getter=_serial(SERIAL))


def test_burn_nonexistent_drive_root_friendly_error(source_dir: Path) -> None:
    """--drive 指了不存在的盘符 (2026-09-14 真机: F: 盘未插, 曾炸裸
    FileNotFoundError) —— 必须是 BurnError 人话, 提示核对盘符。"""
    missing = next((f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    if not Path(f"{c}:\\").exists()), None)
    if missing is None:  # 26 个盘符全被占用的机器上无法模拟
        pytest.skip("无空闲盘符可模拟")
    with pytest.raises(BurnError, match="目标盘不存在"):
        burn(source_dir, Path(missing), b"S" * 64, _opts(), serial_getter=_serial(SERIAL))


# ------------------------------------------------- 查看器内嵌 SEED 比对
# 2026-09 真机: Mac 构建目录缺 seed.bin → build-viewer 静默生成新 SEED →
# .app 内嵌错钥匙, 在 Mac 上恒报"仅限指定的U盘使用"。burn 必须在烧盘前拦下。

def _write_plist(app: Path) -> None:
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": "图书阅览器"}))


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


def _fake_mac_app(base: Path, seed: bytes) -> Path:
    app = base / "图书阅览器.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "图书阅览器").write_bytes(b"FAKE-MACHO")
    _write_plist(app)
    frameworks = app / "Contents" / "Frameworks"
    frameworks.mkdir()
    (frameworks / "seed.bin").write_bytes(seed)
    _write_seal(app)
    return app


def test_burn_mac_viewer_seed_mismatch_blocked(source_dir: Path, drive_dir: Path,
                                               fake_viewer: Path, tmp_path: Path) -> None:
    wrong_app = _fake_mac_app(tmp_path, os.urandom(64))
    with pytest.raises(BurnError, match="内嵌 SEED"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=wrong_app),
             serial_getter=_serial(SERIAL))


def test_burn_mac_viewer_seed_match_ok(source_dir: Path, drive_dir: Path,
                                       fake_viewer: Path, tmp_path: Path) -> None:
    seed = os.urandom(64)
    good_app = _fake_mac_app(tmp_path, seed)
    report = burn(source_dir, drive_dir, seed,
                  _opts(viewer_exe=fake_viewer, viewer_mac=good_app),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_mac_used is not None
    assert (drive_dir / "图书阅览器.app").is_dir()


# ------------------------------------------- macOS 签名封条闸门 (2026-09-25)
# 真机事故 (当日二次修正定谳): 新版 PyInstaller 的 .app 布局含 54 处符号链接,
# zip -r 打包默认跟随链接成副本 + exFAT 无链接语义 —— 中转后封条逐条登记的
# 链接位必失守, 收件人 Mac 弹「已损坏」; 骨架/SEED 检查全部放行。
# 封条级 (CodeResources) 逐文件比对 + 链接条目/嵌套签名条目 (cdhash) 识别才能拦下。


def test_burn_mac_seal_missing_seal_blocked(source_dir: Path, drive_dir: Path,
                                            fake_viewer: Path, tmp_path: Path) -> None:
    app = _fake_mac_app(tmp_path, b"S" * 64)
    shutil.rmtree(app / "Contents" / "_CodeSignature")
    with pytest.raises(BurnError, match="签名封条"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=app),
             serial_getter=_serial(SERIAL))


def test_burn_mac_seal_hash_mismatch_blocked(source_dir: Path, drive_dir: Path,
                                             fake_viewer: Path, tmp_path: Path) -> None:
    """封条之后文件又被改 (哈希不符): SEED 与骨架都看不出, 只有封条能拦。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    lib = app / "Contents" / "Frameworks" / "libfoo.dylib"
    lib.write_bytes(b"v1")
    _write_seal(app)  # 以 v1 内容重新封条
    lib.write_bytes(b"v2")  # 封条之后被改
    with pytest.raises(BurnError, match="哈希不符: Frameworks/libfoo\\.dylib"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=app),
             serial_getter=_serial(SERIAL))


def test_burn_mac_seal_extra_tree_blocked(source_dir: Path, drive_dir: Path,
                                          fake_viewer: Path, tmp_path: Path) -> None:
    """复刻 2026-09-25 真机: 封条外的整套目录树 (Resources/) 混进 bundle。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    shutil.copytree(app / "Contents" / "Frameworks", app / "Contents" / "Resources")
    with pytest.raises(BurnError, match="封条外多余文件: Resources/seed\\.bin"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=app),
             serial_getter=_serial(SERIAL))


def _amend_seal(app: Path, files2_extra: dict[str, dict[str, bytes]]) -> None:
    """在既有伪封条上追加 files2 条目 (链接/嵌套签名等特殊形态)。"""
    seal_path = app / "Contents" / "_CodeSignature" / "CodeResources"
    seal = plistlib.loads(seal_path.read_bytes())
    merged = dict(seal.get("files2") or {})
    merged.update(files2_extra)
    seal["files2"] = merged
    seal_path.write_bytes(plistlib.dumps(seal))


def test_burn_mac_seal_symlink_entry_blocked(source_dir: Path, drive_dir: Path,
                                             fake_viewer: Path, tmp_path: Path) -> None:
    """新版 codesign 用 "symlink" 键登记链接; 中转拍平后 (盘上是普通目录) 必拦。

    2026-09-25(b): 旧闸门只认 "link" 键, 把这类条目误报成"缺失/哈希不符"。
    """
    app = _fake_mac_app(tmp_path, b"S" * 64)
    (app / "Contents" / "Frameworks" / "PIL" / ".dylibs").mkdir(parents=True)
    _amend_seal(app, {"Frameworks/PIL/.dylibs": {"symlink": b"__dot__dylibs"}})
    with pytest.raises(BurnError, match="符号链接失守: Frameworks/PIL/\\.dylibs"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=app),
             serial_getter=_serial(SERIAL))


def test_burn_mac_seal_cdhash_entry_ok(source_dir: Path, drive_dir: Path,
                                       fake_viewer: Path, tmp_path: Path) -> None:
    """嵌套已签名代码条目 (cdhash/requirement, 无内容哈希) 不误报。

    底线检查 = 真 Mach-O (含 fat) 须内嵌签名 SuperBlob 魔数; 完整校验在收件人 Mac。
    """
    app = _fake_mac_app(tmp_path, b"S" * 64)
    lib = app / "Contents" / "Frameworks" / "libsigned.dylib"
    # Mach-O 64 位小端魔数 + SuperBlob 魔数 (嵌入签名在文件内, 位置任意)
    lib.write_bytes(bytes.fromhex("cffaedfe") + b"\x00" * 8
                    + bytes.fromhex("fade0cc0") + b"\x00" * 56)
    _amend_seal(app, {"Frameworks/libsigned.dylib": {
        "cdhash": b"\x01" * 20, "requirement": b"anchor apple generic"}})
    report = burn(source_dir, drive_dir, b"S" * 64,
                  _opts(viewer_exe=fake_viewer, viewer_mac=app),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_mac_used is not None


def test_burn_mac_seal_cdhash_non_macho_data_ok(source_dir: Path, drive_dir: Path,
                                                fake_viewer: Path, tmp_path: Path) -> None:
    """2026-09-29(c) 真机回归: codesign 对 Frameworks 顶层的**非 Mach-O 数据文件**
    同样落 cdhash 条目且不写入嵌入签名 (base_library.zip/seed.bin 实证与
    Resources 侧 hash2 锚定原件逐字节相同) —— 强行要求魔数必误报熔断。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    zipped = app / "Contents" / "Frameworks" / "base_library.zip"
    zipped.write_bytes(b"PK\x03\x04" + b"\x00" * 32)  # zip 头, 非 Mach-O
    _amend_seal(app, {"Frameworks/base_library.zip": {
        "cdhash": b"\x01" * 20, "requirement": b'identifier "base_library"'}})
    report = burn(source_dir, drive_dir, b"S" * 64,
                  _opts(viewer_exe=fake_viewer, viewer_mac=app),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_mac_used is not None


def test_burn_mac_seal_cdhash_tampered_blocked(source_dir: Path, drive_dir: Path,
                                               fake_viewer: Path, tmp_path: Path) -> None:
    """真 Mach-O 的 cdhash 条目文件嵌入签名被剥 (被换/被截) —— 拦下。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    lib = app / "Contents" / "Frameworks" / "libbroken.dylib"
    lib.write_bytes(bytes.fromhex("cffaedfe") + b"mach-o, signature stripped")
    _amend_seal(app, {"Frameworks/libbroken.dylib": {
        "cdhash": b"\x01" * 20, "requirement": b"anchor apple generic"}})
    with pytest.raises(BurnError, match="嵌套签名代码异常.*libbroken\\.dylib"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=app),
             serial_getter=_serial(SERIAL))


def test_burn_mac_seal_bundle_dir_entry_ok(source_dir: Path, drive_dir: Path,
                                           fake_viewer: Path, tmp_path: Path) -> None:
    """2026-09-29(c) 真机回归: 嵌套代码包 (扁平化 Python.framework) 在封条里是
    一条 cdhash **目录**条目 —— 目录在位 + 包内 _CodeSignature/CodeResources 在位
    即过; 包内文件登记在包自己的内封条, 不得误报"缺失/多余"。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    fw = app / "Contents" / "Frameworks" / "Python.framework"
    (fw / "_CodeSignature").mkdir(parents=True)
    (fw / "Python").write_bytes(bytes.fromhex("cffaedfe") + b"PY-BINARY")
    (fw / "Resources").mkdir()
    (fw / "Resources" / "Info.plist").write_bytes(b"PLIST")
    (fw / "_CodeSignature" / "CodeResources").write_bytes(
        plistlib.dumps({"files": {}}))
    _amend_seal(app, {"Frameworks/Python.framework": {
        "cdhash": b"\x01" * 20, "requirement": b'identifier "org.python.python"'}})
    report = burn(source_dir, drive_dir, b"S" * 64,
                  _opts(viewer_exe=fake_viewer, viewer_mac=app),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_mac_used is not None


def test_burn_mac_seal_bundle_missing_inner_seal_blocked(source_dir: Path,
                                                         drive_dir: Path,
                                                         fake_viewer: Path,
                                                         tmp_path: Path) -> None:
    """嵌套代码包目录条目在位但包内没有 _CodeSignature/CodeResources
    (中转丢内封条) —— 拦下, 收件人 Mac 验不了包内完整性。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    fw = app / "Contents" / "Frameworks" / "Python.framework"
    fw.mkdir(parents=True)
    (fw / "Python").write_bytes(b"PY-BINARY")
    _amend_seal(app, {"Frameworks/Python.framework": {
        "cdhash": b"\x01" * 20, "requirement": b'identifier "org.python.python"'}})
    with pytest.raises(BurnError, match="嵌套代码包缺内封条"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=app),
             serial_getter=_serial(SERIAL))


def test_burn_mac_seal_appledouble_residue_ignored(source_dir: Path, drive_dir: Path,
                                                   fake_viewer: Path, tmp_path: Path) -> None:
    """中转残留 ._xxx (AppleDouble) 不算封条外多余文件 (codesign 同口径忽略)。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    (app / "Contents" / "Frameworks" / "._seed.bin").write_bytes(b"apple-double-junk")
    report = burn(source_dir, drive_dir, b"S" * 64,
                  _opts(viewer_exe=fake_viewer, viewer_mac=app),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_mac_used is not None


def test_burn_mac_viewer_missing_seed_bin_rejected(source_dir: Path, drive_dir: Path,
                                                   fake_viewer: Path,
                                                   tmp_path: Path) -> None:
    broken = _fake_mac_app(tmp_path, b"S" * 64)
    (broken / "Contents" / "Frameworks" / "seed.bin").unlink()
    with pytest.raises(BurnError, match="结构异常"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=broken),
             serial_getter=_serial(SERIAL))


def test_burn_mac_viewer_missing_plist_rejected(source_dir: Path, drive_dir: Path,
                                                fake_viewer: Path,
                                                tmp_path: Path) -> None:
    """2026-09 真机: .app 经 Windows 中转被打坏, SEED 一致但 Mac 双击无反应 ——
    烧录前须拦下骨架不完整的 .app, 不能等字节复读 (源和盘一样坏照样全对)。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    (app / "Contents" / "Info.plist").unlink()
    with pytest.raises(BurnError, match="结构异常.*Info\\.plist"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=app),
             serial_getter=_serial(SERIAL))


def test_burn_mac_viewer_mangled_executable_rejected(source_dir: Path, drive_dir: Path,
                                                     fake_viewer: Path,
                                                     tmp_path: Path) -> None:
    """中转时主程序文件名被改写 (zip 解压改码的典型现场): Info.plist 声明的
    可执行文件已不在 —— 双击将无反应, 拒烧。"""
    app = _fake_mac_app(tmp_path, b"S" * 64)
    exe = app / "Contents" / "MacOS" / "图书阅览器"
    exe.rename(exe.with_name("_mangled_by_zip"))
    with pytest.raises(BurnError, match="可执行文件"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=fake_viewer, viewer_mac=app),
             serial_getter=_serial(SERIAL))


def test_burn_win_onedir_seed_mismatch_blocked(source_dir: Path, drive_dir: Path,
                                               tmp_path: Path) -> None:
    onedir = tmp_path / "图书阅览器"
    (onedir / "_internal").mkdir(parents=True)
    (onedir / "_internal" / "seed.bin").write_bytes(os.urandom(64))
    with pytest.raises(BurnError, match="内嵌 SEED"):
        burn(source_dir, drive_dir, b"S" * 64,
             _opts(viewer_exe=onedir), serial_getter=_serial(SERIAL))


def test_burn_on_mac_never_autobuilds_win_viewer(source_dir: Path, drive_dir: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """在 Mac (darwin) 上烧录: 找不到 Windows 产物时绝不自动构建 —— mac 上
    build() 只会产出 .app, 放进 Windows 槽位 = 盘对 Win 收件人静默不可用。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    report = burn(source_dir, drive_dir, b"S" * 64, _opts(),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_exe_used is None
    assert any("无法构建 Windows 查看器" in w for w in report.warnings)


# ------------------------------------------------------------------ 密码模式 (合同⑤)

@pytest.fixture()
def burned_pw(source_dir: Path, drive_dir: Path, fake_viewer: Path,
              tmp_path: Path, dist_isolated: None) -> tuple[Path, str, bytes, Path]:
    seed = os.urandom(64)
    archive = tmp_path / "codes.jsonl"
    report = burn(source_dir, drive_dir, seed,
                  _opts(viewer_exe=fake_viewer, use_password=True, code_archive=archive),
                  serial_getter=_serial(SERIAL))
    assert report.unlock_code is not None
    return drive_dir, report.unlock_code, seed, archive


def test_password_burn_code_and_archive(burned_pw: tuple[Path, str, bytes, Path]) -> None:
    drive, code, _seed, archive = burned_pw
    assert unlock_code.normalize(code) == code  # 烧录报告里的码已是规范形
    ki = inspector.read_keyinfo(drive)
    assert ki.password
    # 归档: 一行一盘, 字段齐全, 文件权限收紧 (新建时 0o600)
    lines = archive.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["code"] == code
    assert rec["title"] == "资料"
    assert rec["serial"] == f"{SERIAL:#010x}"


def test_password_drive_requires_code(burned_pw: tuple[Path, str, bytes, Path]) -> None:
    drive, _code, seed, _archive = burned_pw
    with pytest.raises(inspector.PasswordRequiredError):
        inspector.open_manifest(drive, seed, serial_getter=_serial(SERIAL))


def test_password_wrong_code_fails(burned_pw: tuple[Path, str, bytes, Path]) -> None:
    drive, _code, seed, _archive = burned_pw
    with pytest.raises(inspector.WrongDriveError):
        inspector.open_manifest(drive, seed, serial_getter=_serial(SERIAL),
                                unlock_code=unlock_code.generate())  # 码对但不是本盘的


def test_password_bad_format_code_fails(burned_pw: tuple[Path, str, bytes, Path]) -> None:
    drive, _code, seed, _archive = burned_pw
    with pytest.raises(inspector.WrongDriveError, match="格式"):
        inspector.open_manifest(drive, seed, serial_getter=_serial(SERIAL),
                                unlock_code="bad-code")  # 长度都不对


def test_password_right_code_opens_and_verifies(
        burned_pw: tuple[Path, str, bytes, Path]) -> None:
    drive, code, seed, _archive = burned_pw
    m, _key = inspector.open_manifest(drive, seed, serial_getter=_serial(SERIAL),
                                      unlock_code=f" {code[:4]}-{code[4:]} ".lower())
    assert len(m.files) == 5  # 容忍小写/连字符/首尾空格
    report = inspector.verify_drive(drive, seed, serial_getter=_serial(SERIAL),
                                    unlock_code=code)
    assert report.all_ok


def test_password_clone_with_code_still_fails(
        burned_pw: tuple[Path, str, bytes, Path], tmp_path: Path) -> None:
    """整盘克隆 + 拿到正确码 => 仍解不开 (码只对授权盘有效)。"""
    drive, code, seed, _archive = burned_pw
    clone = tmp_path / "CLONE"
    shutil.copytree(drive, clone)
    _make_writable_tree(drive)
    with pytest.raises(inspector.WrongDriveError):
        inspector.open_manifest(clone, seed, serial_getter=_serial(OTHER_SERIAL),
                                unlock_code=code)


def test_password_archive_failure_raises_with_code(
        source_dir: Path, drive_dir: Path, fake_viewer: Path,
        tmp_path: Path) -> None:
    """归档写不进去 => 烧录报错, 且错误信息内嵌本盘码 (绝不能让码只存在于盘上)。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    bad_archive = blocker / "sub" / "codes.jsonl"  # 父级是文件 => mkdir 必败
    with pytest.raises(BurnError) as ei:
        burn(source_dir, drive_dir, os.urandom(64),
             _opts(viewer_exe=fake_viewer, use_password=True, code_archive=bad_archive),
             serial_getter=_serial(SERIAL))
    assert "手工记录" in str(ei.value)
    assert unlock_code.display(str(ei.value).split("解锁码: ")[-1])  # 报错文本含可解析的码


def test_plain_burn_still_mode0_and_no_code(
        source_dir: Path, drive_dir: Path, fake_viewer: Path,
        tmp_path: Path) -> None:
    """不带 --password: keyinfo mode=0, 报告无码, 不写归档文件。"""
    archive = tmp_path / "codes.jsonl"
    report = burn(source_dir, drive_dir, os.urandom(64),
                  _opts(viewer_exe=fake_viewer, code_archive=archive),
                  serial_getter=_serial(SERIAL))
    assert report.unlock_code is None
    assert report.code_archive is None
    assert not inspector.read_keyinfo(drive_dir).password
    assert not archive.exists()


# ------------------------------------------------------------------ Office 转换分支

def test_office_converted_to_pdf(source_dir: Path, drive_dir: Path, fake_viewer: Path,
                                 tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault import office_convert

    def fake_convert(files: list[Path], out_dir: Path,
                     backend: str | None = None) -> dict[Path, Path]:
        result = {}
        for f in files:
            pdf = out_dir / (f.stem + ".pdf")
            pdf.write_bytes(b"%PDF-converted")
            result[f] = pdf
        return result

    monkeypatch.setattr(office_convert, "missing_apps",
                        lambda files, backend=None: set())
    monkeypatch.setattr(office_convert, "convert_batch", fake_convert)

    seed = os.urandom(64)
    report = burn(source_dir, drive_dir, seed,
                  _opts(viewer_exe=fake_viewer, convert_office=True),
                  serial_getter=_serial(SERIAL))
    assert report.converted == ["合同/合同正文.docx"]
    m, _ = inspector.open_manifest(drive_dir, seed, serial_getter=_serial(SERIAL))
    entry = m.find("合同/合同正文.docx")
    assert entry is not None
    assert entry.media == "pdf"
    assert entry.converted_from == "docx"


def test_office_missing_app_aborts(source_dir: Path, drive_dir: Path, fake_viewer: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault import office_convert

    monkeypatch.setattr(office_convert, "missing_apps",
                        lambda files, backend=None: {"Word.Application"})
    with pytest.raises(BurnError, match="--no-convert"):
        burn(source_dir, drive_dir, os.urandom(64),
             _opts(viewer_exe=fake_viewer, convert_office=True),
             serial_getter=_serial(SERIAL))


# ------------------------------------------------------------------ 前置检查

def test_require_root_rejects_subdir(source_dir: Path, tmp_path: Path, fake_viewer: Path) -> None:
    target = tmp_path / "not" / "root"
    target.parent.mkdir()
    with pytest.raises(BurnError, match="卷根"):
        burn(source_dir, target, os.urandom(64),
             BurnOptions(viewer_exe=fake_viewer), serial_getter=_serial(SERIAL))


def test_empty_source_rejected(tmp_path: Path, drive_dir: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BurnError, match="没有文件"):
        burn(empty, drive_dir, os.urandom(64), _opts(), serial_getter=_serial(SERIAL))


def test_insufficient_space_aborts(source_dir: Path, drive_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    def tiny_free(_p: object) -> object:
        return types.SimpleNamespace(free=100, total=1, used=0)

    monkeypatch.setattr(shutil, "disk_usage", tiny_free)
    with pytest.raises(BurnError, match="空间"):
        burn(source_dir, drive_dir, os.urandom(64), _opts(),
             serial_getter=_serial(SERIAL))


def test_chinese_and_deep_paths(source_dir: Path, drive_dir: Path, fake_viewer: Path) -> None:
    deep = source_dir / "一" / "二" / "三" / "四" / "五"
    deep.mkdir(parents=True)
    (deep / "深层中文文件名.txt").write_text("深", encoding="utf-8")
    seed = os.urandom(64)
    burn(source_dir, drive_dir, seed, _opts(viewer_exe=fake_viewer),
         serial_getter=_serial(SERIAL))
    report = inspector.verify_drive(drive_dir, seed, serial_getter=_serial(SERIAL))
    assert report.all_ok


# ------------------------------------------------------------------ 病毒扫描门

def test_av_threats_abort(source_dir: Path, drive_dir: Path, fake_viewer: Path,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault import av_scan

    outcome = av_scan.ScanOutcome("Windows Defender", 1.0, ["Trojan:Win32/Fake"])
    monkeypatch.setattr(av_scan, "scan_tree", lambda root, **kw: outcome)
    with pytest.raises(BurnError, match="Trojan:Win32/Fake"):
        burn(source_dir, drive_dir, os.urandom(64),
             _opts(viewer_exe=fake_viewer, skip_scan=False),
             serial_getter=_serial(SERIAL))
    assert not (drive_dir / config.DATA_DIR_NAME).exists()  # 中止时不留任何产物


def test_av_unavailable_aborts_fail_closed(source_dir: Path, drive_dir: Path,
                                           fake_viewer: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault import av_scan

    def unavailable(root: Path, **kw: object) -> object:
        raise av_scan.AVUnavailable("未找到 MpCmdRun.exe")
    monkeypatch.setattr(av_scan, "scan_tree", unavailable)
    with pytest.raises(BurnError, match="--skip-scan"):
        burn(source_dir, drive_dir, os.urandom(64),
             _opts(viewer_exe=fake_viewer, skip_scan=False),
             serial_getter=_serial(SERIAL))


def test_av_clean_records_engine(source_dir: Path, drive_dir: Path, fake_viewer: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault import av_scan

    outcome = av_scan.ScanOutcome("Windows Defender", 2.5)
    monkeypatch.setattr(av_scan, "scan_tree", lambda root, **kw: outcome)
    seed = os.urandom(64)
    report = burn(source_dir, drive_dir, seed,
                  _opts(viewer_exe=fake_viewer, skip_scan=False),
                  serial_getter=_serial(SERIAL))
    assert report.av_engine == "Windows Defender"
    assert report.av_duration_s == 2.5
    m, _ = inspector.open_manifest(drive_dir, seed, serial_getter=_serial(SERIAL))
    assert m.av_scanned is not None
    assert m.av_scanned["engine"] == "Windows Defender"


def test_skip_scan_bypasses_and_warns(source_dir: Path, drive_dir: Path,
                                      fake_viewer: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault import av_scan

    def boom(root: Path, **kw: object) -> object:
        raise AssertionError("skip_scan 下不应触发扫描")
    monkeypatch.setattr(av_scan, "scan_tree", boom)
    report = burn(source_dir, drive_dir, os.urandom(64),
                  _opts(viewer_exe=fake_viewer),  # 默认 skip_scan=True
                  serial_getter=_serial(SERIAL))
    assert report.av_engine is None
    assert any("skip-scan" in w for w in report.warnings)


# ------------------------------------------------------------------ 双查看器产物

@pytest.fixture()
def fake_mac_app(tmp_path: Path) -> Callable[[bytes], Path]:
    """伪 .app 工厂: Contents/MacOS/主程序 + Info.plist + 调用方指定内嵌 seed.bin
    (burn 会读它比对烧录 SEED, 缺失/不一致直接拒烧)。"""
    def make(seed: bytes) -> Path:
        app = tmp_path / "mac_dist" / "图书阅览器.app"
        frameworks = app / "Contents" / "Frameworks"
        frameworks.mkdir(parents=True, exist_ok=True)
        (frameworks / "seed.bin").write_bytes(seed)
        macos = app / "Contents" / "MacOS"
        macos.mkdir(parents=True, exist_ok=True)
        (macos / "图书阅览器").write_bytes(b"FAKE-MACHO")
        _write_plist(app)
        _write_seal(app)  # 烧录封条闸门 (2026-09-25) 要求产物带同构封条
        return app
    return make


def test_mac_viewer_tree_copied(source_dir: Path, drive_dir: Path, fake_viewer: Path,
                                fake_mac_app: Callable[[bytes], Path]) -> None:
    seed = os.urandom(64)
    app = fake_mac_app(seed)
    report = burn(source_dir, drive_dir, seed,
                  _opts(viewer_exe=fake_viewer, viewer_mac=app),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_mac_used == app
    exe = drive_dir / "图书阅览器.app" / "Contents" / "MacOS" / "图书阅览器"
    assert exe.read_bytes() == b"FAKE-MACHO"  # 字节级一致
    assert (drive_dir / "图书阅览器.app" / "Contents" / "Info.plist").is_file()


def test_mac_viewer_missing_warns_only(source_dir: Path, drive_dir: Path,
                                       fake_viewer: Path) -> None:
    report = burn(source_dir, drive_dir, os.urandom(64),
                  _opts(viewer_exe=fake_viewer),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_mac_used is None
    assert not (drive_dir / "图书阅览器.app").exists()
    assert any("macOS 查看器" in w for w in report.warnings)


def test_mac_viewer_ascii_build_name_resolved_and_burned_as_chinese(
        source_dir: Path, drive_dir: Path, fake_viewer: Path) -> None:
    """新版 ASCII 产物 vault_viewer.app 放 dist/ 下能被自动找到, 且烧到盘上
    时以中文名 图书阅览器.app 落盘 (收件人看到的名字不变)。"""
    seed = b"S" * 64
    app = _fake_mac_app(config.MODULE_DIR / "dist", seed)  # dist_isolated 后的隔离 dist
    app.rename(app.with_name(f"{config.VIEWER_MAC_BUILD_NAME}.app"))
    report = burn(source_dir, drive_dir, seed,
                  _opts(viewer_exe=fake_viewer),  # 不传 viewer_mac, 走 dist 自动查找
                  serial_getter=_serial(SERIAL))
    assert report.viewer_mac_used is not None
    assert report.viewer_mac_used.name == f"{config.VIEWER_MAC_BUILD_NAME}.app"
    assert (drive_dir / "图书阅览器.app" / "Contents" / "MacOS" / "图书阅览器").is_file()


def test_win_viewer_onedir_copied(source_dir: Path, drive_dir: Path,
                                  tmp_path: Path) -> None:
    onedir = tmp_path / "win_onedir"
    onedir.mkdir()
    (onedir / "图书阅览器.exe").write_bytes(b"ONE-DIR-EXE")
    (onedir / "python314.dll").write_bytes(b"FAKE-DLL")
    report = burn(source_dir, drive_dir, os.urandom(64),
                  _opts(viewer_exe=onedir),
                  serial_getter=_serial(SERIAL))
    assert report.viewer_exe_used == onedir
    assert (drive_dir / "图书阅览器" / "图书阅览器.exe").read_bytes() == b"ONE-DIR-EXE"
    assert (drive_dir / "图书阅览器" / "python314.dll").read_bytes() == b"FAKE-DLL"
    assert not (drive_dir / "图书阅览器.exe").exists()  # 不再有单文件 exe


def test_burn_hides_carrier_artifacts(source_dir: Path, drive_dir: Path,
                                      tmp_path: Path) -> None:
    """密文载体打 +H+S (默认视图与"显示隐藏的文件"均不可见), 查看器 exe 本体
    保持可见可双击; 隐藏掩码整体替换不得丢掉已有只读位。仅 Windows。"""
    if sys.platform != "win32":
        pytest.skip("DOS hidden 属性仅 Windows")
    import ctypes

    onedir = tmp_path / "win_onedir"
    (onedir / "_internal").mkdir(parents=True)
    (onedir / "图书阅览器.exe").write_bytes(b"EXE")
    (onedir / "_internal" / "python314.dll").write_bytes(b"DLL")
    burn(source_dir, drive_dir, os.urandom(64),
         _opts(viewer_exe=onedir), serial_getter=_serial(SERIAL))

    def attrs(p: Path) -> int:
        return int(ctypes.windll.kernel32.GetFileAttributesW(str(p)))

    # 文件: R(来自 set_read_only)+H+S 齐全 —— 隐藏掩码不得冲掉只读位
    hidden_files: list[Path] = [drive_dir / config.KEYINFO_NAME, drive_dir / config.MANIFEST_NAME]
    hidden_files += list((drive_dir / config.DATA_DIR_NAME).glob("*.enc"))
    for p in hidden_files:
        assert p.exists(), f"产物缺失: {p.name}"
        a = attrs(p)
        assert a not in (-1, 0xFFFFFFFF), f"读属性失败: {p.name}"
        assert a & 0x7 == 0x7, f"须为只读+隐藏+系统: {p.name} -> {a:#x}"
    # 目录本身从不设只读 (set_read_only 只动文件), 只断言 H+S
    for p in (drive_dir / config.DATA_DIR_NAME, drive_dir / "图书阅览器" / "_internal"):
        a = attrs(p)
        assert a & 0x6 == 0x6, f"目录未隐藏: {p.name} -> {a:#x}"
    exe = drive_dir / "图书阅览器" / "图书阅览器.exe"
    assert attrs(exe) & 0x2 == 0  # 入口保持可见, 收件人要双击它


@pytest.mark.skipif(sys.platform != "win32", reason="DOS 只读位断言仅 Windows")
def test_viewer_entries_readonly_set(
        source_dir: Path, drive_dir: Path, fake_viewer: Path,
        fake_mac_app: Callable[[bytes], Path]) -> None:
    """2026-09-20 防误删评审收口: 两个端的阅读器入口 (Windows exe +
    macOS .app) 全文件 +R —— 此前只对密文载体显式断言, 阅读器只读只是
    set_read_only 的顺带覆盖。

    **防删声明已撤回 (同日真机+探针双证)**: 资源管理器删除 (单文件/整目录/
    Shift+Del) 走 SHFileOperation, 会先剥只读属性再删, 删得掉; 只读位真正
    挡的是改写/覆写 (DeleteFileW 直调与写打开被拒, 见下方 PermissionError)
    与 cmd del / Remove-Item 不带强制参数的删除。误删恢复靠备份或重烧。
    """
    import ctypes

    seed = os.urandom(64)
    burn(source_dir, drive_dir, seed,
         _opts(viewer_exe=fake_viewer, viewer_mac=fake_mac_app(seed)),
         serial_getter=_serial(SERIAL))

    def attrs(p: Path) -> int:
        return int(ctypes.windll.kernel32.GetFileAttributesW(str(p)))

    def readonly(p: Path) -> None:
        assert p.exists(), f"产物缺失: {p}"
        assert attrs(p) & 0x1 == 0x1, f"缺只读位: {p.name} -> {attrs(p):#x}"

    readonly(drive_dir / "图书阅览器.exe")  # Windows 端入口 (onefile)
    app = drive_dir / "图书阅览器.app"  # macOS 端入口 (单入口时同卷)
    readonly(app / "Contents" / "MacOS" / "图书阅览器")
    readonly(app / "Contents" / "Info.plist")
    readonly(app / "Contents" / "Frameworks" / "seed.bin")

    # DeleteFileW 直调对只读文件拒绝 (改写类防护的契约锚点; 注意这不代表
    # 资源管理器路径 —— 见本测试 docstring 的撤回声明)。
    with pytest.raises(PermissionError):
        os.remove(drive_dir / "图书阅览器.exe")
    with pytest.raises(PermissionError):
        os.remove(app / "Contents" / "MacOS" / "图书阅览器")


def test_reburn_removes_stale_viewer_trees(source_dir: Path, drive_dir: Path,
                                           fake_viewer: Path,
                                           fake_mac_app: Callable[[bytes], Path]) -> None:
    seed1 = os.urandom(64)
    burn(source_dir, drive_dir, seed1,
         _opts(viewer_exe=fake_viewer, viewer_mac=fake_mac_app(seed1)),
         serial_getter=_serial(SERIAL))
    _make_writable_tree(drive_dir)
    # 第二次不携带 mac 查看器 => 旧 .app 目录必须被清掉
    burn(source_dir, drive_dir, os.urandom(64),
         _opts(viewer_exe=fake_viewer),
         serial_getter=_serial(SERIAL))
    assert not (drive_dir / "图书阅览器.app").exists()
    assert len(list((drive_dir / config.DATA_DIR_NAME).glob("*.enc"))) == 5


def test_viewer_copy_corruption_detected(source_dir: Path, drive_dir: Path,
                                         fake_viewer: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """拷贝途中字节损坏 => 复读校验拦截 (模拟坏盘/掉盘)。"""
    real = shutil.copyfile

    def bad_copy(src: Path, dst: Path) -> Path:
        real(src, dst)
        with open(dst, "ab") as fh:
            fh.write(b"\x00")  # 多写一字节
        return dst
    monkeypatch.setattr("src.app.usb_vault.burner.shutil.copyfile", bad_copy)
    with pytest.raises(BurnError, match="复读校验失败"):
        burn(source_dir, drive_dir, os.urandom(64),
             _opts(viewer_exe=fake_viewer),
             serial_getter=_serial(SERIAL))


# ------------------------------------------------------------------ 工具函数

def test_extended_path() -> None:
    short = Path("C:/short/file.txt")
    assert extended_path(short) == str(short)
    p = Path("C:/dir") / ("x" * 280)  # 远超 MAX_PATH
    ep = extended_path(p)
    assert ep.startswith(chr(92) * 2 + "?" + chr(92))
    assert ep.endswith("x" * 280)
