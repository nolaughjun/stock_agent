"""CLI parser 单测: 新参数与子命令接线 (不执行实际动作)。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from src.app.usb_vault import config, inspector, main as cli
from src.app.usb_vault.disk_layout import DiskSummary


def test_burn_new_flags_parsed() -> None:
    args = cli.build_parser().parse_args([
        "burn", "--source", "S", "--drive", "E:",
        "--viewer-mac", "/x/图书阅览器.app", "--converter", "soffice",
        "--viewer-mac-intel", "/x/vault_viewer_intel.app",
        "--av-cmd", "myav {path}", "--skip-scan",
    ])
    assert args.viewer_mac == "/x/图书阅览器.app"
    assert args.viewer_mac_intel == "/x/vault_viewer_intel.app"
    assert args.converter == "soffice"
    assert args.av_cmd == "myav {path}"
    assert args.skip_scan is True


def test_burn_defaults_unchanged() -> None:
    args = cli.build_parser().parse_args(["burn", "--source", "S", "--drive", "E:"])
    assert args.viewer_mac is None
    assert args.viewer_mac_intel is None
    assert args.converter is None
    assert args.av_cmd is None
    assert args.skip_scan is False


def test_av_scan_subcommand() -> None:
    args = cli.build_parser().parse_args(["av-scan", "--target", "T", "--timeout", "60"])
    assert args.target == "T"
    assert args.timeout == 60.0


def test_build_viewer_new_flags() -> None:
    args = cli.build_parser().parse_args(["build-viewer", "--vt-build", "--sign"])
    assert args.vt_build is True
    assert args.sign == ""  # 省略值 => 自动选证书
    assert args.onefile is False  # 默认 onedir


def test_build_viewer_sign_with_identity() -> None:
    args = cli.build_parser().parse_args(
        ["build-viewer", "--sign", "C:/certs/my.pfx"])
    assert args.sign == "C:/certs/my.pfx"


# ---- 首跑 ImportError 人话提示 (2026-09 真机: Mac 只拷 usb_vault 一层 + 包内跑 main.py) --

@pytest.mark.parametrize("name", ["src", "src.app", "src.app.usb_vault"])
def test_import_hint_package_chain_lists_both_run_modes(name: str) -> None:
    hint = cli._import_error_hint(
        ModuleNotFoundError(f"No module named '{name}'", name=name))
    assert "src.app.usb_vault.main" in hint  # a) 完整仓库姿势
    assert "python3 main.py" in hint  # b) 单目录姿势


def test_import_hint_incomplete_directory() -> None:
    exc = ModuleNotFoundError("No module named 'src.app.usb_vault.volume_id'",
                              name="src.app.usb_vault.volume_id")
    hint = cli._import_error_hint(exc)
    assert "拷贝不完整" in hint
    assert "volume_id" in hint
    assert "完整拷贝" in hint


def test_import_hint_tkinter_brew() -> None:
    hint = cli._import_error_hint(ModuleNotFoundError("No module named 'tkinter'",
                                                      name="tkinter"))
    assert "python-tk" in hint


def test_import_hint_generic_dependency() -> None:
    hint = cli._import_error_hint(ModuleNotFoundError("No module named 'cryptography'",
                                                      name="cryptography"))
    assert "cryptography" in hint
    assert "pip install" in hint


# ---- 版本闸门 (2026-09-14 Mac 真机: Xcode 自带 python3=3.9, 依赖装齐仍死在
#      manifest.py 的 @dataclass(slots=True) —— 3.10 才有的参数) ----

def test_python_version_gate_rejects_3_9() -> None:
    err = cli._python_version_error((3, 9, 18))
    assert err is not None
    assert "3.10" in err  # 说清最低版本
    assert "slots" in err  # 点名用户即将 google 的那句 TypeError
    assert "Xcode" in err  # Mac 现场元凶提示
    assert "python@3.12" in err  # brew 安装指引
    assert "main.py" in err  # 装好后跑什么


@pytest.mark.parametrize("v", [(3, 10), (3, 10, 0), (3, 12, 7), (3, 13, 0)])
def test_python_version_gate_accepts_modern(v: tuple[int, ...]) -> None:
    assert cli._python_version_error(v) is None


def test_standalone_dir_runs_without_repo(tmp_path: Path) -> None:
    """单目录模式回归: 只拷 usb_vault/ (无 src/app 祖先链) 也能跑起来。

    复刻 2026-09 Mac 真机现场 —— 子进程在独立目录里执行 main.py --help,
    自举把该目录挂到 src.app.usb_vault 包位后整条 import 链必须走通。
    (剥掉 PYTHONPATH, 防止仓库根目录在环境里而假通过。)"""
    dst = tmp_path / "usb_vault"
    shutil.copytree(config.MODULE_DIR, dst,
                    ignore=shutil.ignore_patterns("__pycache__", "dist", "secrets",
                                                  "tests"))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "main.py", "--help"], cwd=dst, env=env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in (proc.stdout or "")
    assert "ModuleNotFoundError" not in (proc.stderr or "")


# ---- --dual-entry / --yes (双分区烧录, 实验性; 磁盘动作在 test_burner_dual) ----

_WIPE_SUMMARY = DiskSummary(3, "SanDisk Ultra", 32 * 1024 ** 3, "USB", ["E"])


def test_burn_dual_flags_parsed() -> None:
    args = cli.build_parser().parse_args(
        ["burn", "--source", "S", "--drive", "E:", "--dual-entry", "--yes"])
    assert args.dual_entry is True
    assert args.yes is True


def test_burn_dual_flags_default_off() -> None:
    args = cli.build_parser().parse_args(["burn", "--source", "S", "--drive", "E:"])
    assert args.dual_entry is False
    assert args.yes is False


def test_wipe_confirm_assume_yes_skips_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """--yes 豁免: input 决不能被碰到 (批处理烧录场景)。"""

    def boom(prompt: str = "") -> str:
        raise AssertionError("--yes 不应触发交互输入")

    monkeypatch.setattr("builtins.input", boom)
    assert cli._ask_wipe_confirmation(_WIPE_SUMMARY, assume_yes=True) is True


@pytest.mark.parametrize("answer", ["yes", "  yes  "])
def test_wipe_confirm_accepts_literal_yes(
        answer: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: answer)
    assert cli._ask_wipe_confirmation(_WIPE_SUMMARY, assume_yes=False) is True


@pytest.mark.parametrize("answer", ["YES", "y", "是", "no", ""])
def test_wipe_confirm_rejects_lookalikes(
        answer: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """全盘清空前的最后闸: 一切"看起来像 yes"的变体一律拒绝。"""
    monkeypatch.setattr("builtins.input", lambda _p: answer)
    assert cli._ask_wipe_confirmation(_WIPE_SUMMARY, assume_yes=False) is False


def test_wipe_confirm_prompt_shows_disk_details(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: "no")
    cli._ask_wipe_confirmation(_WIPE_SUMMARY, assume_yes=False)
    out = capsys.readouterr().out
    assert "磁盘 3" in out and "SanDisk Ultra" in out  # 磁盘号 + 型号
    assert "32GB" in out and "USB" in out  # 容量 + 总线
    assert "全盘清空" in out and "E" in out  # 后果 + 现有盘符


# ---- heal / verify 分区2隐藏态 (2026-09-29 跨平台自愈) ----
# macOS 改写 GPT 剥掉无盘符属性位 → 收件 Windows 给分区2补盘符; 烧录机
# verify 报警 + heal 免重烧恢复。接线层测试, 磁盘动作在 test_disk_layout。

class _FakeReport:
    title = "测试资料"
    results: list[Any] = []
    all_ok = True


def test_heal_flags_parsed() -> None:
    args = cli.build_parser().parse_args(["heal", "--drive", "E:"])
    assert args.drive == "E:"
    assert args.func is cli.cmd_heal


def test_cmd_heal_rejects_foreign_drive(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = cli.build_parser().parse_args(["heal", "--drive", str(tmp_path)])
    with pytest.raises(SystemExit, match="不是资料盘"):
        cli.cmd_heal(args)


def test_cmd_heal_single_partition_needs_nothing(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    from src.app.usb_vault import disk_layout
    from src.app.usb_vault.burner import _ALL_VIEWER_DIR_NAMES

    (tmp_path / _ALL_VIEWER_DIR_NAMES[0]).mkdir()
    monkeypatch.setattr(disk_layout, "require_admin", lambda: None)
    monkeypatch.setattr(disk_layout, "_letter_of", lambda p: "E")
    monkeypatch.setattr(disk_layout, "resolve_disk",
                        lambda letter: _WIPE_SUMMARY)
    monkeypatch.setattr(disk_layout, "partition2_state", lambda dn: None)
    args = cli.build_parser().parse_args(["heal", "--drive", str(tmp_path)])
    assert cli.cmd_heal(args) == 0
    assert "单分区形态" in capsys.readouterr().out


def test_cmd_heal_restores_hidden_partition2(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    from src.app.usb_vault import disk_layout
    from src.app.usb_vault.burner import _ALL_VIEWER_DIR_NAMES

    (tmp_path / _ALL_VIEWER_DIR_NAMES[0]).mkdir()
    monkeypatch.setattr(disk_layout, "require_admin", lambda: None)
    monkeypatch.setattr(disk_layout, "_letter_of", lambda p: "E")
    monkeypatch.setattr(disk_layout, "resolve_disk",
                        lambda letter: _WIPE_SUMMARY)
    monkeypatch.setattr(disk_layout, "partition2_state",
                        lambda dn: {"letters": ["K"], "is_gpt": True, "attr_ok": False})
    healed: list[int] = []

    def fake_heal(dn: int) -> list[str]:
        healed.append(dn)
        return ["K"]

    monkeypatch.setattr(disk_layout, "heal_partition2_hidden", fake_heal)
    args = cli.build_parser().parse_args(["heal", "--drive", str(tmp_path)])
    assert cli.cmd_heal(args) == 0
    assert healed == [3]  # 动的是 resolve_disk 定位到的磁盘号
    out = capsys.readouterr().out
    assert "分区2隐藏已恢复" in out and "K" in out


def _fake_verify_env(monkeypatch: pytest.MonkeyPatch, p2_state: object) -> None:
    """cmd_verify 接线环境: 哈希与 ACL 段全绿, 只留分区2态可变。"""
    from src.app.usb_vault import disk_layout, ntfs_guard

    monkeypatch.setattr(cli, "_load_seed_arg", lambda path: b"seed")
    monkeypatch.setattr(inspector, "verify_drive",
                        lambda drive, seed, unlock_code=None: _FakeReport())
    monkeypatch.setattr(ntfs_guard, "is_ntfs", lambda root: False)
    monkeypatch.setattr(disk_layout, "disk_number_of", lambda letter: 3)
    monkeypatch.setattr(disk_layout, "partition2_state", lambda dn: p2_state)


@pytest.mark.parametrize("p2_state, expect", [
    ({"letters": [], "is_gpt": True, "attr_ok": True}, 0),   # 健康双分区
    (None, 0),                                                # 单分区: 无可查
])
def test_cmd_verify_p2_healthy_or_absent(
        monkeypatch: pytest.MonkeyPatch, p2_state: object, expect: int,
        capsys: pytest.CaptureFixture[str]) -> None:
    _fake_verify_env(monkeypatch, p2_state)
    args = cli.build_parser().parse_args(["verify", "--drive", "E:"])
    assert cli.cmd_verify(args) == expect
    out = capsys.readouterr().out
    if p2_state is not None:
        assert "分区2隐藏属性: 在位 ✔" in out


def test_cmd_verify_p2_stripped_fails_with_heal_hint(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """macOS 剥属性后的现场: verify 必须报警退出非零, 并给出 heal 恢复指引。"""
    _fake_verify_env(
        monkeypatch, {"letters": ["K"], "is_gpt": True, "attr_ok": False})
    args = cli.build_parser().parse_args(["verify", "--drive", "E:"])
    assert cli.cmd_verify(args) == 1
    out = capsys.readouterr().out
    assert "分区2隐藏形态: 已失效" in out
    assert "盘符 K" in out and "heal --drive" in out


def test_cmd_verify_p2_mbr_legacy_warns_only(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_verify_env(
        monkeypatch, {"letters": [], "is_gpt": False, "attr_ok": False})
    args = cli.build_parser().parse_args(["verify", "--drive", "E:"])
    assert cli.cmd_verify(args) == 0  # 旧 MBR 制式只降级告警, 不拦出仓
    assert "旧 MBR 制式" in capsys.readouterr().out
