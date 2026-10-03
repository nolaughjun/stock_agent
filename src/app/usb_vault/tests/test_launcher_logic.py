"""书名引导壳 (v2.4.5) 逻辑测试: 转交判据 / 谢幕分支 / 参数透传。

背景: onedir 真主程序被单独拷走后死在 PyInstaller C 装载器层
("Failed to load Python DLL ... 找不到指定模块"), v2.4.4 应用层门禁跑不到;
书名 exe 改为自包含 onefile 引导壳后才轮到本模块的逻辑生效。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.app.usb_vault import config
from src.app.usb_vault.viewer import app as viewer_app
from src.app.usb_vault.viewer import launcher_entry


# ---------------------------------------------------------------------------
# 谢幕文案: 单一事实源在 config, app.py 别名同源 (两处永不漂移)
# ---------------------------------------------------------------------------


def test_detached_message_single_source() -> None:
    """v2.4.5: 文案原文照录锁 config.DETACHED_MESSAGE; app.py 只做别名。"""
    assert config.DETACHED_MESSAGE == "脱离U盘介质，加密书稿无法在该设备上打开查看"
    assert viewer_app.DETACHED_MESSAGE is config.DETACHED_MESSAGE


def test_broken_install_message_locked() -> None:
    """v2.4.6: 坏盘文案与谢幕文案必须可分辨 (真棒事故: 真主程序被删被误报
    成"脱离U盘介质", 误导排障方向)。"""
    assert config.BROKEN_INSTALL_MESSAGE == "阅览器文件不完整，请联系发件方重新烧录U盘"
    assert config.BROKEN_INSTALL_MESSAGE != config.DETACHED_MESSAGE


# ---------------------------------------------------------------------------
# resolve_target: 目录完整 → 真主程序; 残缺 → None
# ---------------------------------------------------------------------------


def test_resolve_target_complete_dir_returns_real_exe(tmp_path: Path) -> None:
    (tmp_path / config.VIEWER_EXE_NAME).write_bytes(b"EXE")
    (tmp_path / "_internal").mkdir()
    assert launcher_entry.resolve_target(tmp_path) == tmp_path / config.VIEWER_EXE_NAME


def test_resolve_target_missing_internal_returns_none(tmp_path: Path) -> None:
    """书名 exe 被单独拷走 (旁边没有 _internal): 按脱离介质谢幕。"""
    (tmp_path / config.VIEWER_EXE_NAME).write_bytes(b"EXE")
    assert launcher_entry.resolve_target(tmp_path) is None


def test_resolve_target_missing_real_exe_returns_none(tmp_path: Path) -> None:
    """壳和 _internal 都在但真主程序缺失 (半截拷贝): 同样谢幕, 不猜。"""
    (tmp_path / "_internal").mkdir()
    assert launcher_entry.resolve_target(tmp_path) is None


def test_resolve_target_ignores_directory_name(tmp_path: Path) -> None:
    """壳只认"旁边的主程序+_internal", 不认目录名 —— 整目录改名搬走不影晌转交。"""
    renamed = tmp_path / "随便改名的文件夹"
    renamed.mkdir()
    (renamed / config.VIEWER_EXE_NAME).write_bytes(b"EXE")
    (renamed / "_internal").mkdir()
    assert launcher_entry.resolve_target(renamed) == renamed / config.VIEWER_EXE_NAME


# ---------------------------------------------------------------------------
# main: 转交 (0) / 谢幕 (1)
# ---------------------------------------------------------------------------


def test_main_handoff_spawns_real_exe_with_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / config.VIEWER_EXE_NAME
    target.write_bytes(b"EXE")
    (tmp_path / "_internal").mkdir()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "书名.exe"))  # onefile 冻结形态
    spawned: list[tuple[list[str], str]] = []

    def _fake_popen(cmd: list[str], cwd: str | None = None) -> int:
        spawned.append((cmd, str(cwd)))
        return 0

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)  # launcher_entry 用的同一模块
    assert launcher_entry.main(["--selfcheck", "额外参数"]) == 0
    assert spawned == [([str(target), "--selfcheck", "额外参数"], str(tmp_path))]


def test_main_detached_shows_message_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """壳被单独拷走 (无 _internal): 弹谢幕文案。"""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "书名.exe"))  # 无 _internal 旁伴随
    monkeypatch.setattr(launcher_entry, "resolve_target", lambda here, name=None: None)
    shown: list[str] = []
    monkeypatch.setattr(
        launcher_entry, "show_message_box", lambda text: shown.append(text)
    )
    assert launcher_entry.main() == 1
    assert shown == [config.DETACHED_MESSAGE]


def test_main_broken_install_shows_distinct_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2.4.6 真棒事故形态: _internal 在而真主程序缺 (被删/被杀毒隔离) ——
    这是坏盘不是拷离, 必须弹"文件不完整"而非误导性的"脱离U盘介质"。"""
    (tmp_path / "_internal").mkdir()  # 依赖树在, 真主程序不在
    monkeypatch.setattr(sys, "executable", str(tmp_path / "书名.exe"))
    monkeypatch.setattr(launcher_entry, "resolve_target", lambda here, name=None: None)
    shown: list[str] = []
    monkeypatch.setattr(
        launcher_entry, "show_message_box", lambda text: shown.append(text)
    )
    assert launcher_entry.main() == 1
    assert shown == [config.BROKEN_INSTALL_MESSAGE]


def test_main_detached_non_win32_prints_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """理论路径 (壳只在 Windows 构建): 非 win32 保底把文案送到 stdout。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(launcher_entry, "resolve_target", lambda here, name=None: None)
    assert launcher_entry.main() == 1
    assert config.DETACHED_MESSAGE in capsys.readouterr().out


def test_show_message_box_uses_messageboxw_text_verbatim() -> None:
    """提示框走原生 MessageBoxW, 文案/标题直传 (win32 独占断言)。

    ctypes 在 show_message_box 函数体内导入 (非 win32 平台 import 安全),
    测试侧自行 import 后替换 windll.user32.MessageBoxW。
    """
    if sys.platform != "win32":
        pytest.skip("win32 MessageBoxW 真实断言")
    import ctypes

    calls: list[tuple[int, str, str, int]] = []
    user32 = ctypes.windll.user32
    orig = user32.MessageBoxW

    def _fake_box(hwnd: int, text: str, title: str, flags: int) -> int:
        calls.append((hwnd, text, title, flags))
        return 1

    user32.MessageBoxW = _fake_box  # type: ignore[attr-defined]
    try:
        launcher_entry.show_message_box(config.BROKEN_INSTALL_MESSAGE)
    finally:
        user32.MessageBoxW = orig  # type: ignore[attr-defined]
    assert calls == [(0, config.BROKEN_INSTALL_MESSAGE, "图书阅览器", 0x40)]


def test_launcher_module_has_no_heavy_dependency() -> None:
    """壳的依赖红线: 只许 stdlib 轻量件 + 包链 (AST 导入白名单, 注释不算) ——
    产物 ~10MB、解包亚秒, 杀毒签名面不随壳扩张 (build_launcher 不做任何
    --exclude-module, 全靠入口干净)。"""
    import ast

    tree = ast.parse(Path(launcher_entry.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "subprocess", "sys", "pathlib", "ctypes", "src"}, imported
