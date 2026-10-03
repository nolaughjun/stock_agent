"""build_viewer 纯逻辑单测: 版本资源文本 / 假 SEED / 产物名 (不跑 PyInstaller)。"""

from __future__ import annotations

import os
import plistlib
import struct
import sys
from pathlib import Path

import pytest

from src.app.usb_vault.config import MODULE_DIR, VIEWER_VERSION
from src.app.usb_vault.viewer import build_viewer  # build_viewer.sys 即全局 sys


def test_vt_fake_seed_shape() -> None:
    assert len(build_viewer.VT_FAKE_SEED) == 64
    assert b"NEVER-DISTRIBUTE" in build_viewer.VT_FAKE_SEED


def test_version_file_content(tmp_path: Path) -> None:
    vf = tmp_path / "version.txt"
    build_viewer._write_version_file(vf, "图书阅览器")
    text = vf.read_text(encoding="utf-8")
    assert "VSVersionInfo" in text
    assert "图书阅览器 (只读)" in text
    assert VIEWER_VERSION in text
    assert "图书阅览器.exe" in text
    assert "2052, 1200" in text  # 简体中文资源表


def test_version_file_rejects_bad_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_viewer, "VIEWER_VERSION", "x.y")
    with pytest.raises(build_viewer.BuildError, match="VIEWER_VERSION"):
        build_viewer._write_version_file(Path("x") / "v.txt", "n")


def test_build_rejects_bad_seed_length(tmp_path: Path) -> None:
    with pytest.raises(build_viewer.BuildError, match="64 字节"):
        build_viewer.build(tmp_path, b"short")


def test_build_refuses_without_tkinter(monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path) -> None:
    """2026-09-18 真机回归: 构建机 Python 缺 tkinter 时 PyInstaller 静默出炉,
    产物启动即 `No module named 'tkinter'` 闪退 —— 构建期熔断。"""
    monkeypatch.setitem(sys.modules, "tkinter", None)  # import 即 ImportError
    with pytest.raises(build_viewer.BuildError, match="缺 tkinter"):
        build_viewer.build(tmp_path, b"x" * 64)


def test_default_artifact_name_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert build_viewer.default_artifact_name() == "图书阅览器"
    monkeypatch.setattr(sys, "platform", "darwin")
    # mac 特意用 ASCII 名: 中文名 .app 经 zip 中转会被 Windows 按本地码页改写
    # 文件名 (2026-09 真机: 旧名"资料查看器" → 璧勬枡鏌ョ湅鍣, .app 双击无反应)
    name = build_viewer.default_artifact_name()
    assert name == "vault_viewer"
    assert name.isascii()


def test_build_args_stage_entry_and_excludes(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """回归 (2026-09-14 真机事故):

    1. 入口脚本必须是复制到临时目录的副本 —— 直接传 src/app/.../app.py 时
       PyInstaller 会把包链顶层 src/app 加进隔离子进程 PYTHONPATH, 其中本地
       traceback/ 包遮蔽标准库, 子进程 ImportError 暴毙 (SubprocessDiedError);
    2. 必须带 --exclude-module pandas/torch —— pymupdf.table 的可选 pandas
       导入会把整个 ML/Web 栈 (837MB) 拖进产物。
    """
    import subprocess
    import types

    captured_args: list[str] = []
    captured_entry = b""

    def fake_run(args: list[str], **kw: object) -> object:
        nonlocal captured_args, captured_entry
        captured_args = list(args)
        # 临时目录在 build() 返回后即被清理, 内容必须在调用现场抓取
        captured_entry = Path(args[-1]).read_bytes()
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)  # 全局模块对象, build_viewer 内生效
    out = tmp_path / "dist"
    (out / "图书阅览器").mkdir(parents=True)
    build_viewer.build(out, b"S" * 64)

    argv = captured_args
    entry = Path(argv[-1])
    assert entry.name == "viewer_entry.py"
    # 在临时目录, 不在仓库包链里 (tempfile 前缀恰好含 usb_vault, 不能查子串)
    assert not entry.parent.is_relative_to(MODULE_DIR)
    assert captured_entry == (MODULE_DIR / "viewer" / "app.py").read_bytes()
    excludes = {argv[i + 1] for i, a in enumerate(argv) if a == "--exclude-module"}
    assert {"pandas", "torch", "transformers", "numpy"} <= excludes
    hidden = {argv[i + 1] for i, a in enumerate(argv) if a == "--hidden-import"}
    assert "src.app.usb_vault.unlock_code" in hidden


def test_build_stages_package_chain_for_standalone(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """回归 (2026-09-15 真机): 单目录独立运行形态 (Mac 只拷 usb_vault 目录) 下
    真实 src/app/usb_vault 包链不存在, modulegraph 解析不了 hidden-import →
    产物缺包, 收件人端 No module named 'src' (--windowed 一闪即退, 双击"无反应")。
    build 必须暂存 stage/src/app/usb_vault 链 (--paths 指向它)。
    """
    import shutil
    import subprocess
    import types

    fake_module_dir = tmp_path / "usb_vault"  # 伪"没有仓库链"的独立目录
    for py in MODULE_DIR.rglob("*.py"):
        if "tests" in py.relative_to(MODULE_DIR).parts:
            continue
        rel = py.relative_to(MODULE_DIR)
        (fake_module_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(py, fake_module_dir / rel)
    monkeypatch.setattr(build_viewer, "MODULE_DIR", fake_module_dir)

    staged: list[Path] = []

    def fake_run(args: list[str], **kw: object) -> object:
        vals = [Path(args[i + 1]) for i, a in enumerate(args) if a == "--paths"]
        # 临时目录在 build() 返回后即被清理, 现场验证暂存链完整可解析
        staged[:] = [v for v in vals
                     if (v / "src" / "app" / "usb_vault" / "config.py").is_file()]
        assert staged, f"--paths 里没有可解析的包链: {vals}"
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = tmp_path / "dist"
    (out / "图书阅览器").mkdir(parents=True)
    build_viewer.build(out, b"S" * 64)  # fake_run 内部已断言暂存链在场


def test_build_fails_on_missing_hidden_import(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """熔断: PyInstaller 对没解析到的 hidden-import 只打 WARNING 且 returncode=0,
    坏产物会静默出仓 —— build 必须扫输出, 发现自家模块缺失当场报错。"""
    import subprocess
    import types

    def fake_run(args: list[str], **kw: object) -> object:
        return types.SimpleNamespace(
            returncode=0, stdout="",
            stderr="121 WARNING: Hidden import 'src.app.usb_vault.config' not found",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(build_viewer.BuildError, match="未解析到"):
        build_viewer.build(tmp_path, b"S" * 64)


def test_sign_artifact_darwin_identity_carries_notarization_flags(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Developer ID 正式签名必须带 --options runtime (加固运行时, 公证硬前提)
    与 --timestamp (Apple 对 Developer ID 的硬要求), 少一个 notarytool 必拒;
    ad-hoc ("-", 无身份) 两者都不带 —— 只求 Apple Silicon 可跑。
    2026-09-24 购入 Developer ID 后定案 (此前只产 ad-hoc, 无法免 Gatekeeper 提示)。
    """
    import subprocess
    import types

    monkeypatch.setattr(sys, "platform", "darwin")
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **kw: object) -> object:
        captured[argv[argv.index("--sign") + 1]] = argv
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert build_viewer.sign_artifact(Path("a.app"))[0]
    assert build_viewer.sign_artifact(Path("a.app"), "Developer ID Application: X (T1)")[0]

    adhoc = captured["-"]
    assert "--options" not in adhoc and "--timestamp" not in adhoc
    formal = captured["Developer ID Application: X (T1)"]
    assert formal[formal.index("--options") + 1] == "runtime"
    assert "--timestamp" in formal


def test_patch_mac_bundle_metadata_makes_identifier_notarizable(
        tmp_path: Path) -> None:
    """公证硬前提: Info.plist 必须有合法 CFBundleIdentifier (下划线不合法,
    PyInstaller CLI 无 spec 时落的是占位值) 与版本号。补丁必须发生在签名前
    —— plist 属于被签内容。identifier 从产物名派生且跨版本稳定。"""
    import plistlib

    app = tmp_path / "vault_viewer.app" / "Contents"
    app.mkdir(parents=True)
    original = {"CFBundleExecutable": "vault_viewer", "CFBundleIdentifier": "com.ToFi.ToFi"}
    (app / "Info.plist").write_bytes(plistlib.dumps(original))

    build_viewer.patch_mac_bundle_metadata(app.parent, "vault_viewer")

    data = plistlib.loads((app / "Info.plist").read_bytes())
    assert data["CFBundleIdentifier"] == "com.usbvault.vault-viewer"  # 下划线→连字符
    assert "_" not in data["CFBundleIdentifier"]
    assert data["CFBundleShortVersionString"] == VIEWER_VERSION
    assert data["CFBundleVersion"] == VIEWER_VERSION
    assert data["CFBundleExecutable"] == "vault_viewer"  # 其余键不动


def test_deref_mac_symlinks_flattens_file_and_dir_links(tmp_path: Path) -> None:
    """签名前去链接化 (2026-09-25(b)): 新版 PyInstaller 布局 54 处链接,
    zip 跟随 + exFAT 无链接语义两头失守 —— 拍平成真实拷贝后封条登记纯文件树。
    (h) 后仍走拷贝的只剩两类: Frameworks 顶层文件链接 (@rpath/<纯文件名>
    解析靠它) 与 Resources 内部指向 Resources 的数据链接; Resources 侧
    指向 Frameworks 的镜像 (代码) 一律删, 见 (h) 专项测试。

    Windows 测试机造不了真符号链接 (需开发者模式), is_symlink/readlink 注入伪链接。
    """
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    (fw / "pymupdf").mkdir(parents=True)
    (fw / "pymupdf" / "libmupdf.dylib").write_bytes(b"MUPDF")
    res = app / "Contents" / "Resources"
    (res / "share" / "tcl").mkdir(parents=True)
    (res / "share" / "tcl" / "init.tcl").write_bytes(b"TCL")
    # 伪链接 1 (文件, 成链途经目录链接): Frameworks/libmupdf.dylib
    #   -> mupdfdir/libmupdf.dylib (mupdfdir 本身是目录链接 -> pymupdf)
    fake_file_link = fw / "libmupdf.dylib"
    fake_file_link.write_bytes(b"")  # 占位 (真符号链接在测试机上造不出)
    fake_dir_link = fw / "mupdfdir"  # 伪目录链接 → pymupdf ((c)(d) 规则删)
    fake_dir_link.mkdir()
    # 伪链接 2 (数据树内链接, Resources→Resources): share/tcl.link -> tcl
    fake_data_link = res / "share" / "tcl.link"
    fake_data_link.write_bytes(b"")
    targets: dict[Path, str] = {
        fake_file_link: "mupdfdir/libmupdf.dylib",
        fake_dir_link: "pymupdf",
        fake_data_link: "tcl",
    }

    def is_fake_link(p: Path) -> bool:
        # 伪链接 = 空占位文件/空目录; 被拍平后 (有真实内容) 即不再是链接
        if p not in targets:
            return False
        return not any(p.iterdir()) if p.is_dir() else p.stat().st_size == 0

    n = build_viewer._deref_mac_symlinks(
        app, is_symlink=is_fake_link, readlink=lambda p: targets[p])

    assert n == 3  # 1 处目录链接删除 + 2 处拷贝
    assert fake_file_link.read_bytes() == b"MUPDF"  # 顶层文件链接 → 真实拷贝 (成链解析到真身)
    assert (fake_data_link / "init.tcl").read_bytes() == b"TCL"  # 数据链接 → 整树拷贝
    assert not fake_dir_link.exists()  # Frameworks 目录链接 → 删除 (codesign 毒)


def test_deref_deletes_dotted_dir_link_in_frameworks(tmp_path: Path) -> None:
    """2026-09-26(c) Mac 真机: v1 把 Frameworks/PIL/.dylibs 目录链接拷成真目录,
    codesign 必拒 (`bundle format unrecognized ... PIL/.dylibs`)。三分法:
    带点目录链接在 Frameworks 下 → 删除; 途经它的顶层文件链接
    (libXau.6.dylib → PIL/.dylibs/libXau.6.dylib) 先从真身 __dot__dylibs
    拷成真文件 —— PyInstaller 6.22 (utils/osx.py) 把所有非系统依赖改写成
    `@rpath/<纯文件名>`, 目录分量不进 load command, 删除后无引用残留。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    (fw / "PIL" / "__dot__dylibs").mkdir(parents=True)
    (fw / "PIL" / "__dot__dylibs" / "libXau.6.dylib").write_bytes(b"XAU")
    res = app / "Contents" / "Resources"
    res.mkdir()
    fake_dotted_dir = fw / "PIL" / ".dylibs"  # 伪目录链接 → __dot__dylibs
    fake_dotted_dir.mkdir()
    # 顶层文件链接在 Frameworks 侧 (@rpath/<纯文件名> 解析锚点, (h) 照拷;
    # Resources 侧的镜像链接归 (h) 规则删, 见专项测试)
    fake_top_file = fw / "libXau.6.dylib"
    fake_top_file.write_bytes(b"")
    targets: dict[Path, str] = {
        fake_dotted_dir: "__dot__dylibs",
        fake_top_file: "PIL/.dylibs/libXau.6.dylib",
    }

    def is_fake_link(p: Path) -> bool:
        if p not in targets:
            return False
        return not any(p.iterdir()) if p.is_dir() else p.stat().st_size == 0

    n = build_viewer._deref_mac_symlinks(
        app, is_symlink=is_fake_link, readlink=lambda p: targets[p])

    assert n == 2
    assert fake_top_file.read_bytes() == b"XAU"  # 顶层文件链接 → 真身拷贝
    assert not fake_dotted_dir.exists()  # 带点目录链接 → 删除 (codesign 毒)
    # 真身不动, 封条照常登记
    assert (fw / "PIL" / "__dot__dylibs" / "libXau.6.dylib").read_bytes() == b"XAU"


def test_deref_deletes_resources_code_mirrors_pointing_into_frameworks(
        tmp_path: Path) -> None:
    """2026-09-29(h) 公证收口回归 (真机 notarytool Invalid, log 全指向
    Contents/Resources/PIL/.dylibs/*.dylib): PyInstaller 双树布局里二进制
    真树在 Frameworks, Resources 侧只放镜像链接 (目录镜像 Resources/PIL、
    顶层文件镜像 Resources/libwebp.6.dylib)。旧规则"Resources 下普通链接
    照拷"把镜像落成真拷贝 —— 外层 codesign --deep 只签代码位置
    (MacOS/Frameworks), Resources 下的散装 Mach-O 只封哈希不签名 →
    公证逐条拒 "The binary is not signed with a valid Developer ID
    certificate"。运行时 MEIPASS=Frameworks, 无人从 Resources 装载代码
    → 目录/文件镜像一律删除。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    (fw / "PIL" / "__dot__dylibs").mkdir(parents=True)
    (fw / "PIL" / "__dot__dylibs" / "libwebp.6.dylib").write_bytes(b"WEBP")
    res = app / "Contents" / "Resources"
    res.mkdir()
    fake_dir_mirror = res / "PIL"  # 伪目录镜像链接 → ../Frameworks/PIL
    fake_dir_mirror.mkdir()
    fake_file_mirror = res / "libwebp.6.dylib"  # 伪顶层文件镜像链接
    fake_file_mirror.write_bytes(b"")
    fake_dotted = fw / "PIL" / ".dylibs"  # 伪目录链接 → __dot__dylibs ((c) 规则删)
    fake_dotted.mkdir()
    fake_fw_top = fw / "libwebp.6.dylib"  # 伪顶层文件链接, 途经 .dylibs ((c) 规则拷)
    fake_fw_top.write_bytes(b"")
    targets: dict[Path, str] = {
        fake_dir_mirror: "../Frameworks/PIL",
        fake_file_mirror: "../Frameworks/libwebp.6.dylib",
        fake_dotted: "__dot__dylibs",
        fake_fw_top: "PIL/.dylibs/libwebp.6.dylib",
    }

    def is_fake_link(p: Path) -> bool:
        if p not in targets:
            return False
        return not any(p.iterdir()) if p.is_dir() else p.stat().st_size == 0

    n = build_viewer._deref_mac_symlinks(
        app, is_symlink=is_fake_link, readlink=lambda p: targets[p])

    assert n == 4  # 2 处镜像删除 + 1 处 (c) 链接删除 + 1 处 (c) 顶层拷贝
    assert not fake_dir_mirror.exists()  # 目录镜像 → 删, 不落未签名代码副本
    assert not fake_file_mirror.exists()  # 顶层文件镜像 → 同删
    assert not fake_dotted.exists()
    # @rpath 解析锚点仍在 Frameworks 顶层, 真身不动
    assert fake_fw_top.read_bytes() == b"WEBP"
    assert (fw / "PIL" / "__dot__dylibs" / "libwebp.6.dylib").read_bytes() == b"WEBP"


def test_deref_invariant_rejects_real_dotted_dir_in_frameworks(tmp_path: Path) -> None:
    """拍平后不变量: Frameworks 下不许有"名字带点的真目录" (唯一豁免 .framework
    及其内部)。违反 = 拍平策略漏网或产物被动过 —— 当场熔断报路径,
    不等 codesign 报一句难懂的 bundle format unrecognized。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    (fw / "PIL" / ".dylibs").mkdir(parents=True)
    (fw / "PIL" / ".dylibs" / "x.dylib").write_bytes(b"X")
    # .framework 内部的 3.14 带点目录 = 合法嵌套, 不得误报
    (fw / "Python.framework" / "Versions" / "3.14").mkdir(parents=True)

    with pytest.raises(build_viewer.BuildError, match="真目录"):
        build_viewer._deref_mac_symlinks(app)


def test_deref_invariant_rejects_binaries_in_resources(tmp_path: Path) -> None:
    """(h) 公证前置不变量: Resources 下不许有散装 .dylib/.so 真文件 ——
    codesign --deep 只签代码位置 (MacOS/Frameworks), Resources 下的二进制
    只封进封条哈希不签名, 公证必拒 "not signed with a valid Developer ID
    certificate"。真身落在这 = 拍平漏网或产物被动过, 当场熔断,
    别等公证被拒才发现 (2026-09-29 真机 Invalid 教训)。"""
    app = tmp_path / "vault_viewer.app"
    res = app / "Contents" / "Resources"
    (res / "PIL" / ".dylibs").mkdir(parents=True)
    (res / "PIL" / ".dylibs" / "libwebp.6.dylib").write_bytes(b"WEBP")

    with pytest.raises(build_viewer.BuildError, match="Resources 下存在二进制"):
        build_viewer._deref_mac_symlinks(app)


def test_deref_deletes_data_mirror_dir_link_in_frameworks(tmp_path: Path) -> None:
    """2026-09-26(d) 收口回归 (真机 "opt0.4"): Frameworks 下**不带点**目录链接
    (tcl8 类数据镜像软链, 真身在 Resources) 走拷贝时, copytree 把 Resources 侧
    带点子目录 (8.4/8.6 这类) 原样搬进 Frameworks → codesign 毒。定谳
    (PyInstaller osx.py Step 2): 这类链接全是纯观感镜像, 运行期无人寻址
    (依赖全走 @rpath/<纯文件名> → Frameworks 顶层真实文件) → 一律删除。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    res = app / "Contents" / "Resources"
    (res / "tcl8" / "8.4").mkdir(parents=True)
    (res / "tcl8" / "8.4" / "platform.tm").write_bytes(b"TM")
    fw.mkdir()
    fake_mirror = fw / "tcl8"  # 伪目录链接 (不带点名) → ../Resources/tcl8
    fake_mirror.mkdir()

    n = build_viewer._deref_mac_symlinks(
        app,
        is_symlink=lambda p: p == fake_mirror and not any(p.iterdir()),
        readlink=lambda p: "../Resources/tcl8",
    )

    assert n == 1
    assert not fake_mirror.exists()  # 数据镜像 → 删除, 不是拷贝
    # 真身 (含带点子目录) 在 Resources 原地不动, Resources 侧无禁令
    assert (res / "tcl8" / "8.4" / "platform.tm").read_bytes() == b"TM"


def test_deref_flattens_framework_bundle_to_flat_layout(tmp_path: Path) -> None:
    """2026-09-26(f) 四连收口回归 (真机 `bundle format unrecognized … In
    subcomponent: …/Frameworks/Python.framework`): (e) 删光框架内链接后,
    无 Current 无根级别名的 Versions/<ver>/ 残架 codesign 仍拒签
    (PyInstaller utils/osx.py:672 注释自认 Current 对 codesign 必要)。
    链接过不了 zip/exFAT 中转 → 唯一活路 = 扁平化: 二进制与 Resources
    提到框架根, Versions/ 整体移除。规范第 4 条是条件式 (没有 Versions
    就没有"旁边不许有他物"), 经典扁平框架合法。运行时不受影响:
    bootloader dlopen 的是 Frameworks 顶层锚点真文件。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    framework = fw / "Python.framework"
    ver = framework / "Versions" / "3.14"
    ver.mkdir(parents=True)
    (ver / "Python").write_bytes(b"PY")
    (ver / "Resources").mkdir()
    (ver / "Resources" / "Info.plist").write_bytes(b"PLIST")
    fake_current = framework / "Versions" / "Current"  # 伪目录链接 → 3.14
    fake_current.mkdir()
    # 框架根的文件别名 (Python.framework/Python → Versions/Current/Python):
    # 真文件站在 Versions/ 旁边同样违规 ((e) ambiguous 元凶) → 删除
    fw_root_alias = framework / "Python"
    fw_root_alias.write_bytes(b"")
    targets: dict[Path, str] = {
        fake_current: "3.14",
        fw_root_alias: "Versions/Current/Python",
    }

    def is_fake_link(p: Path) -> bool:
        if p not in targets:
            return False
        return not any(p.iterdir()) if p.is_dir() else p.stat().st_size == 0

    n = build_viewer._deref_mac_symlinks(
        app, is_symlink=is_fake_link, readlink=lambda p: targets[p])

    assert n == 4  # 2 处链接删除 + 2 条扁平化搬移 (Python + Resources)
    assert not fake_current.exists()
    # 框架根别名 (fw_root_alias) 路径如今站着版本化真身 —— 扁平化的本意;
    # 读到 b"PY" 而非空占位, 恰证明别名已消失、真身已上位
    assert (framework / "Python").read_bytes() == b"PY"
    assert (framework / "Resources" / "Info.plist").read_bytes() == b"PLIST"
    assert not (framework / "Versions").exists()


def test_deref_flattens_top_level_anchor_and_keeps_flat_framework(tmp_path: Path) -> None:
    """(f) 配套: bindepend 给非顶层二进制造的 Frameworks 顶层链接
    (Python → Python.framework/Versions/<ver>/Python) 是 bootloader 的
    dlopen 锚点 (api.py 存纯 basename, pyi_dylib_python.c 裸拼接路径,
    无框架魔法) → 拍平成真文件后框架内部布局无人在意; 本来就扁平的
    .framework (部分 wheel 如此) 不再动。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    framework = fw / "Python.framework"
    framework.mkdir(parents=True)
    (framework / "Python").write_bytes(b"REALBODY")
    (framework / "Resources").mkdir()
    (framework / "Resources" / "Info.plist").write_bytes(b"PLIST")
    fake_anchor = fw / "Python"  # 伪文件链接 → 框架真身 (bootloader 锚点)
    fake_anchor.write_bytes(b"")
    anchor_before = (framework / "Python").read_bytes()

    n = build_viewer._deref_mac_symlinks(
        app,
        is_symlink=lambda p: p == fake_anchor and p.stat().st_size == 0,
        readlink=lambda p: "Python.framework/Python",
    )

    assert n == 1  # 仅锚点链接拍平; 已扁平框架零搬移
    assert fake_anchor.read_bytes() == anchor_before  # 锚点 = 真文件拷贝
    assert (framework / "Python").read_bytes() == anchor_before  # 框架真身不动
    assert (framework / "Resources" / "Info.plist").read_bytes() == b"PLIST"
    assert not (framework / "Versions").exists()


def test_deref_deletes_framework_named_mirror_link_in_resources(tmp_path: Path) -> None:
    """(e) 同源毒: Resources/Python.framework→../Frameworks/Python.framework
    整框架镜像链接若落成拷贝, copytree 跟随内层链接会在拷贝内部再造
    "真目录站在 Versions/ 旁边"的违规结构; 运行期也无人从 Resources 加载
    框架 → 删除, 不拷。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    (fw / "Python.framework" / "Versions" / "3.14").mkdir(parents=True)
    (fw / "Python.framework" / "Versions" / "3.14" / "Python").write_bytes(b"PY")
    res = app / "Contents" / "Resources"
    res.mkdir()
    fake = res / "Python.framework"  # 伪目录链接 → ../Frameworks/Python.framework
    fake.mkdir()

    n = build_viewer._deref_mac_symlinks(
        app,
        is_symlink=lambda p: p == fake and not any(p.iterdir()),
        readlink=lambda p: "../Frameworks/Python.framework",
    )

    assert n == 2  # 1 处镜像删除 + 1 条扁平化搬移 (Python)
    assert not fake.exists()  # 整框架镜像 → 删除, 不在 Resources 落违规拷贝
    # (f) 链接删净后框架同步扁平化 (镜像若被拷贝会在拷贝内部再造违规结构)
    assert (fw / "Python.framework" / "Python").read_bytes() == b"PY"
    assert not (fw / "Python.framework" / "Versions").exists()


def test_deref_hoists_lib_dynload_to_frameworks_top(tmp_path: Path) -> None:
    """2026-09-26(g) 直跑收口回归 (真机 ModuleNotFoundError: No module named
    '_struct'): PyInstaller 把 lib-dynload 扩展落盘名改写成 python3.14/
    lib-dynload/… (building/utils.py:95-101), .app 里靠 "python3.14 →
    python3__dot__14" 点名替换链接寻址 (拷成真目录 = codesign 点名关必拒,
    只能删)。struct 由 bootloader 最先导入的 pyimod01_archive 触发、早于
    一切钩子 → 唯一修法 = 把 .so 提到 Frameworks 顶层 (sys.path 首项,
    FileFinder 落盘探测)。Resources 侧 python3.14 镜像同删不留死副本。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    real = fw / "python3__dot__14" / "lib-dynload"
    real.mkdir(parents=True)
    (real / "_struct.cpython-314-darwin.so").write_bytes(b"SO1")
    (real / "math.cpython-314-darwin.so").write_bytes(b"SO2")
    res = app / "Contents" / "Resources"
    res.mkdir()
    fake_fw_link = fw / "python3.14"  # 伪目录链接 → 点名替换真身目录
    fake_fw_link.mkdir()
    fake_res_mirror = res / "python3.14"  # 伪目录镜像链接 → ../Frameworks/python3.14
    fake_res_mirror.mkdir()
    targets: dict[Path, str] = {
        fake_fw_link: "python3__dot__14",
        fake_res_mirror: "../Frameworks/python3.14",
    }

    def is_fake_link(p: Path) -> bool:
        if p not in targets:
            return False
        return not any(p.iterdir())

    n = build_viewer._deref_mac_symlinks(
        app, is_symlink=is_fake_link, readlink=lambda p: targets[p])

    assert n == 4  # 2 处链接删除 + 2 条 .so 提升搬移
    assert not fake_fw_link.exists()
    assert not fake_res_mirror.exists()  # 镜像删除, 不留无人寻址的死副本
    assert not (fw / "python3__dot__14").exists()  # 提升后空壳树移除
    # .so 站上 Frameworks 顶层 = MEIPASS (sys.path 首项), 导入器自然命中
    assert (fw / "_struct.cpython-314-darwin.so").read_bytes() == b"SO1"
    assert (fw / "math.cpython-314-darwin.so").read_bytes() == b"SO2"


def test_deref_hoist_collision_fuses(tmp_path: Path) -> None:
    """lib-dynload 提升遇顶层撞名 = 产物疑被外部动过, 熔断报路径,
    不让运行时报一句难懂的 ModuleNotFoundError。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    real = fw / "python3__dot__14" / "lib-dynload"
    real.mkdir(parents=True)
    (real / "_struct.cpython-314-darwin.so").write_bytes(b"NEW")
    (fw / "_struct.cpython-314-darwin.so").write_bytes(b"OLD")  # 顶层已有同名
    fake_fw_link = fw / "python3.14"
    fake_fw_link.mkdir()

    with pytest.raises(build_viewer.BuildError, match="提升撞名"):
        build_viewer._deref_mac_symlinks(
            app,
            is_symlink=lambda p: p == fake_fw_link and not any(p.iterdir()),
            readlink=lambda p: "python3__dot__14",
        )


def test_mac_runtime_hook_redirects_tcl_to_resources(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-26(g) 配套: _tcl_data/_tk_data 真身在 Resources, Frameworks
    侧镜像链接已删 (拷贝会把树内带点子目录搬进 Frameworks 拒签); 自带钩子
    pyi_rth__tkinter 只认 MEIPASS/_tcl_data, 目录不在时静默不设环境变量 →
    本钩子按 .app 布局把 TCL_LIBRARY/TK_LIBRARY 指回 Resources 真树。
    非 .app 布局 (无 Contents/Resources 之分) 必须整体空转。"""
    hook_path = tmp_path / "pyi_rth_usbvault_mac_dirs.py"
    build_viewer._write_mac_runtime_hook(hook_path)
    source = hook_path.read_text(encoding="utf-8")
    compile(source, str(hook_path), "exec")  # 生成物必须是合法 Python

    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    res = app / "Contents" / "Resources"
    (res / "_tcl_data").mkdir(parents=True)  # _tk_data 刻意不存在
    fw.mkdir()
    monkeypatch.delenv("TCL_LIBRARY", raising=False)
    monkeypatch.delenv("TK_LIBRARY", raising=False)

    monkeypatch.setattr(sys, "_MEIPASS", str(fw), raising=False)
    exec(compile(source, str(hook_path), "exec"), {})  # noqa: S102 - 受控生成物
    assert os.environ["TCL_LIBRARY"] == str(res / "_tcl_data")
    assert "TK_LIBRARY" not in os.environ  # 目录不存在 → 不设
    # 裸 pop 清场: monkeypatch.delenv 会把 exec 设的值登记为"旧值"并在
    # teardown 还原 —— TCL_LIBRARY 泄漏进后续测试, tk.Tk() 找不到 init.tcl
    os.environ.pop("TCL_LIBRARY", None)

    onedir = tmp_path / "onedir"  # 非 .app 布局: 无 Contents/Resources 之分
    onedir.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(onedir), raising=False)
    exec(compile(source, str(hook_path), "exec"), {})  # noqa: S102
    assert "TCL_LIBRARY" not in os.environ


def test_deref_mac_symlinks_broken_link_raises(tmp_path: Path) -> None:
    """断链 (目标不存在) 是产物残缺, 必须熔断而不是静默拍平。"""
    app = tmp_path / "vault_viewer.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    link = contents / "Frameworks" / "seed.bin"
    link.parent.mkdir()
    link.write_bytes(b"placeholder")
    with pytest.raises(build_viewer.BuildError, match="断链"):
        build_viewer._deref_mac_symlinks(
            app,
            is_symlink=lambda p: p == link,
            readlink=lambda p: "../Resources/seed.bin",  # 目标不存在
        )


def test_deref_mac_symlinks_no_links_is_noop(tmp_path: Path) -> None:
    """无链接的产物 (旧版 PyInstaller 平铺布局) 原样返回, 计数为 0。"""
    app = tmp_path / "vault_viewer.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "vault_viewer").write_bytes(b"MACHO")
    assert build_viewer._deref_mac_symlinks(app) == 0


# --- 2026-09-29(i) rpath 归一化 + 依赖自包含/scrypt 双不变量 -----------------
# 真机事故: 直跑开窗正常, 输解锁码后崩 AttributeError: module 'hashlib'
# has no attribute 'scrypt' —— (g) 提升与拍平搬移没跟着改字节里的 rpath
# 算术, _hashlib 在顶层再上一层解析到 Contents/ 加载失败, hashlib 静默
# 降级 (sha256 走内置 _sha2 照常, 唯独 scrypt 消失)。


def _fake_macho(dylibs: list[str] | None = None,
                rpaths: list[str] | None = None) -> bytes:
    """最小可解析 64 位小端 arm64 MH_DYLIB (rpath 归一化/解析不变量测试)。"""
    cmds = b""
    for name in dylibs or []:
        payload = name.encode() + b"\x00"
        size = 24 + len(payload)
        pad = (-size) % 8
        cmds += (struct.pack("<II", 0xC, size + pad)
                 + struct.pack("<I", 24) + struct.pack("<III", 0, 0, 0)
                 + payload + b"\x00" * pad)
    for rp in rpaths or []:
        payload = rp.encode() + b"\x00"
        size = 12 + len(payload)
        pad = (-size) % 8
        cmds += (struct.pack("<II", 0x8000001C, size + pad)
                 + struct.pack("<I", 12) + payload + b"\x00" * pad)
    ncmds = len(dylibs or []) + len(rpaths or [])
    return struct.pack("<IIIIIIII", 0xFEEDFACF, 0x100000C, 0, 6, ncmds,
                       len(cmds), 0, 0) + cmds


def _macho_refs(raw: bytes) -> tuple[list[str], list[str]]:
    scan = build_viewer._macho_scan(raw)
    assert scan is not None
    return scan[1], [name for _off, _start, name in scan[2]]


def test_macho_scan_parses_fixture_roundtrip() -> None:
    deps, rpaths = _macho_refs(_fake_macho(
        dylibs=["@rpath/libcrypto.3.dylib", "/usr/lib/libSystem.B.dylib"],
        rpaths=["@loader_path/../..", "@executable_path/../Frameworks"]))
    assert deps == ["@rpath/libcrypto.3.dylib", "/usr/lib/libSystem.B.dylib"]
    assert rpaths == ["@loader_path/../..", "@executable_path/../Frameworks"]
    assert build_viewer._macho_scan(b"PK\x03\x04zzzz") is None  # 非 Mach-O


def test_macho_scan_fuses_truncated_load_cmds() -> None:
    raw = _fake_macho(rpaths=["@loader_path/../.."])
    with pytest.raises(build_viewer.BuildError, match="截断|异常"):
        build_viewer._macho_scan(raw[:40])  # 头里声明了命令但字节不够


def test_fix_mac_rpaths_rewrites_lifted_copy(tmp_path: Path) -> None:
    """被提升到顶层的副本: ../.. 差一层 → 原位改写回 @loader_path。

    PyInstaller 契约 = 真身按自然深度落盘 + rpath 按自身深度指向顶层 +
    顶层别名链接兜解析; 拍平拷出顶层副本后 rpath 算术即断, 修复 = 改写
    rpath 指回顶层 (只缩短、NUL 填充、载入命令长度不变、依赖声明不动)。
    """
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    fw.mkdir(parents=True)
    (fw / "libcrypto.3.dylib").write_bytes(_fake_macho())  # 依赖目标在顶层
    lifted = fw / "_hashlib.cpython-314-darwin.so"
    before = _fake_macho(dylibs=["@rpath/libcrypto.3.dylib"],
                         rpaths=["@loader_path/../.."])
    lifted.write_bytes(before)

    assert build_viewer._fix_mac_rpaths(app) == 1
    after = lifted.read_bytes()
    assert len(after) == len(before)  # 载入命令长度不变 (原位填充)
    deps, rpaths = _macho_refs(after)
    assert deps == ["@rpath/libcrypto.3.dylib"]  # 依赖声明本身不动
    assert rpaths == ["@loader_path"]
    # 改写后全包解析必须自包含
    build_viewer._assert_mac_dylib_resolution(app)


def test_fix_mac_rpaths_deep_original_and_bare_untouched(tmp_path: Path) -> None:
    """原位真身 (深处 ../.. 本来就指对) 与顶层裸 @loader_path 零改动。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    deep = fw / "PIL" / "__dot__dylibs" / "libtiff.6.dylib"
    deep.parent.mkdir(parents=True)
    deep_bytes = _fake_macho(rpaths=["@loader_path/../.."])
    deep.write_bytes(deep_bytes)
    (fw / "_tkinter.cpython-314-darwin.so").write_bytes(
        _fake_macho(rpaths=["@loader_path"]))
    (fw / "libtcl9.0.dylib").write_bytes(_fake_macho())

    assert build_viewer._fix_mac_rpaths(app) == 0
    assert deep.read_bytes() == deep_bytes


def test_fix_mac_rpaths_needs_growth_fuses(tmp_path: Path) -> None:
    """改写需变长 (无法原位) = 布局超出预期, 熔断报详情。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    sub = fw / "pymupdf"
    sub.mkdir(parents=True)
    # 一层深却持裸 @loader_path (指向自己目录), 归一化需 ../ = 变长
    (sub / "_mupdf.so").write_bytes(
        _fake_macho(dylibs=["@rpath/libmupdf.dylib"], rpaths=["@loader_path"]))

    with pytest.raises(build_viewer.BuildError, match="增长"):
        build_viewer._fix_mac_rpaths(app)


def test_fix_then_assert_reproduces_real_incident_layout(tmp_path: Path) -> None:
    """复刻真机事故布局 (顶层提升副本 + 深处真身 + pymupdf 顶层镜像):
    修一处全包自包含, 未修则不变量精确点名每一处断链。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    deep_dir = fw / "PIL" / "__dot__dylibs"
    deep_dir.mkdir(parents=True)
    deep_bytes = _fake_macho(
        dylibs=["@rpath/libjpeg.62.4.0.dylib", "/usr/lib/libSystem.B.dylib"],
        rpaths=["@loader_path/../.."])
    deep = deep_dir / "libtiff.6.dylib"
    deep.write_bytes(deep_bytes)  # 原位真身, 本来就对
    (fw / "libtiff.6.dylib").write_bytes(
        _fake_macho(dylibs=["@rpath/libjpeg.62.4.0.dylib"],
                    rpaths=["@loader_path/../.."]))  # (c) 拷出的顶层副本
    (fw / "libjpeg.62.4.0.dylib").write_bytes(_fake_macho())
    (fw / "_hashlib.cpython-314-darwin.so").write_bytes(
        _fake_macho(dylibs=["@rpath/libcrypto.3.dylib"],
                    rpaths=["@loader_path/../.."]))  # (g) 提升的顶层副本
    (fw / "libcrypto.3.dylib").write_bytes(_fake_macho())
    (fw / "libmupdf.dylib").write_bytes(_fake_macho())
    (fw / "libmupdfcpp.so").write_bytes(
        _fake_macho(dylibs=["@rpath/libmupdf.dylib"],
                    rpaths=["@loader_path/.."]))  # 从 pymupdf/ 拷上来的
    (fw / "_struct.cpython-314-darwin.so").write_bytes(
        _fake_macho(dylibs=["/usr/lib/libSystem.B.dylib"]))  # 无依赖者不受累

    # 未修: 不变量逐一点名 (libtiff/_hashlib/libmupdfcpp 三处)
    with pytest.raises(build_viewer.BuildError, match="解析不自包含") as ei:
        build_viewer._assert_mac_dylib_resolution(app)
    assert "libtiff.6.dylib" in str(ei.value)
    assert "_hashlib" in str(ei.value)
    assert "libmupdfcpp.so" in str(ei.value)

    assert build_viewer._fix_mac_rpaths(app) == 3
    build_viewer._assert_mac_dylib_resolution(app)  # 修后全绿
    assert deep.read_bytes() == deep_bytes  # 深处真身始终没动


def test_assert_mac_dylib_resolution_rejects_non_hermetic(tmp_path: Path) -> None:
    """外部绝对路径与裸文件名依赖都非自包含交付形态, 一律熔断。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    fw.mkdir(parents=True)
    (fw / "_ssl.cpython-314-darwin.so").write_bytes(
        _fake_macho(dylibs=["/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib"]))
    with pytest.raises(build_viewer.BuildError, match="仅构建机"):
        build_viewer._assert_mac_dylib_resolution(app)

    (fw / "_ssl.cpython-314-darwin.so").write_bytes(
        _fake_macho(dylibs=["libcrypto.3.dylib"]))  # 裸文件名
    with pytest.raises(build_viewer.BuildError, match="裸文件名"):
        build_viewer._assert_mac_dylib_resolution(app)

    (fw / "_ssl.cpython-314-darwin.so").write_bytes(
        _fake_macho(dylibs=["/usr/lib/libSystem.B.dylib",
                            "/System/Library/Frameworks/CoreFoundation.framework/"
                            "Versions/A/CoreFoundation"]))
    build_viewer._assert_mac_dylib_resolution(app)  # 系统库放行


def test_assert_mac_dylib_resolution_honors_main_exe_rpath(
        tmp_path: Path) -> None:
    """主程序 rpath 参与解析 (dyld 行为对齐); Info.plist 坏则优雅跳过。"""
    app = tmp_path / "vault_viewer.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    fw = contents / "Frameworks"
    macos.mkdir(parents=True)
    fw.mkdir()
    (contents / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleExecutable": "vault_viewer"}))
    (macos / "vault_viewer").write_bytes(
        _fake_macho(rpaths=["@loader_path/../Frameworks"]))
    # 自身无 rpath, 只能靠主程序 rpath 解析
    (fw / "_decimal.cpython-314-darwin.so").write_bytes(
        _fake_macho(dylibs=["@rpath/libmpdec.4.dylib"]))
    (fw / "libmpdec.4.dylib").write_bytes(_fake_macho())
    build_viewer._assert_mac_dylib_resolution(app)

    (contents / "Info.plist").write_bytes(b"not-a-plist")
    # 坏 plist → 主程序 rpath 这一路没了 → 同一棵树断言熔断: 反证上一行
    # 的通过确实走了主程序 rpath, 而非碰巧命中
    with pytest.raises(build_viewer.BuildError, match="_decimal"):
        build_viewer._assert_mac_dylib_resolution(app)


def test_assert_mac_scrypt_ready_gates(tmp_path: Path) -> None:
    """解锁链前置: _hashlib 与 libcrypto 缺一或不含 scrypt 均熔断。"""
    app = tmp_path / "vault_viewer.app"
    fw = app / "Contents" / "Frameworks"
    fw.mkdir(parents=True)
    with pytest.raises(build_viewer.BuildError, match="缺 _hashlib"):
        build_viewer._assert_mac_scrypt_ready(app)

    (fw / "_hashlib.cpython-314-darwin.so").write_bytes(b"hashlib-without-kdf")
    with pytest.raises(build_viewer.BuildError, match="LibreSSL"):
        build_viewer._assert_mac_scrypt_ready(app)

    (fw / "_hashlib.cpython-314-darwin.so").write_bytes(b"xxscryptxx")
    with pytest.raises(build_viewer.BuildError, match="缺 libcrypto"):
        build_viewer._assert_mac_scrypt_ready(app)

    (fw / "libcrypto.3.dylib").write_bytes(b"openssl-no-kdf-here")
    with pytest.raises(build_viewer.BuildError, match="不含 scrypt"):
        build_viewer._assert_mac_scrypt_ready(app)

    (fw / "libcrypto.3.dylib").write_bytes(b"openssl scrypt ok")
    build_viewer._assert_mac_scrypt_ready(app)
