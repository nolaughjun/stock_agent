"""build_viewer 纯逻辑单测: 版本资源文本 / 假 SEED / 产物名 (不跑 PyInstaller)。"""

from __future__ import annotations

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
