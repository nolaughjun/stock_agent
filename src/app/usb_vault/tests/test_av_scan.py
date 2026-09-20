"""av_scan 单测: 退出码矩阵 / 引擎探测 / 威胁解析 / fail-closed (全 mock, 不跑真引擎)。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.app.usb_vault import av_scan

# 注: av_scan.sys / av_scan.subprocess 即全局模块对象, 直接 patch 全局等价


class _FakeRun:
    """按预设应答的 subprocess.run 替身; 记录调用参数。"""

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "",
                 raises: Exception | None = None) -> None:
        self.rc, self.stdout, self.stderr, self.raises = rc, stdout, stderr, raises
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kw: object) -> object:
        self.calls.append(argv)
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


# ------------------------------------------------------------------ Defender

def test_defender_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=0)
    monkeypatch.setattr(
        av_scan, "defender_cmd_path",
        lambda: tmp_path / "MpCmdRun.exe")
    monkeypatch.setattr(subprocess, "run", fake)
    out = av_scan.scan_tree(tmp_path)
    assert out.clean
    assert out.engine == "Windows Defender"
    argv = fake.calls[0]
    assert "-DisableRemediation" in argv  # 检测不处置
    assert str(tmp_path) in argv


def test_defender_threats_parsed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=2, stdout=(
        "Scan started\n"
        "Threat                : Trojan:Win32/Fake\n"
        "Threat                : Virus:Win32/Other\n"))
    monkeypatch.setattr(av_scan, "defender_cmd_path", lambda: Path("C:/x/MpCmdRun.exe"))
    monkeypatch.setattr(subprocess, "run", fake)
    out = av_scan.scan_tree(tmp_path)
    assert not out.clean
    assert out.threats == ["Trojan:Win32/Fake", "Virus:Win32/Other"]


def test_defender_threat_no_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=2, stdout="found something")
    monkeypatch.setattr(av_scan, "defender_cmd_path", lambda: Path("C:/x/MpCmdRun.exe"))
    monkeypatch.setattr(subprocess, "run", fake)
    out = av_scan.scan_tree(tmp_path)
    assert len(out.threats) == 1
    assert "人工复核" in out.threats[0]


def test_defender_engine_failure_is_not_a_threat(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """2026-09-17 真机回归: MpCmdRun 故障也吐退出码 2 ("CmdTool: Failed
    with hr = 0x80004005", 用时 0.0s 秒退) —— 必须归类引擎故障 (AVScanError,
    指向本机 Defender), 而不是"检出威胁"(指向源目录)。
    """
    fake = _FakeRun(rc=2, stdout=(
        "CmdTool: Failed with hr = 0x80004005. "
        "Check C:\\Users\\sun5k\\AppData\\Local\\Temp\\MpCmdRun.log "
        "for more information\n"))
    monkeypatch.setattr(av_scan, "defender_cmd_path", lambda: Path("C:/x/MpCmdRun.exe"))
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(av_scan.AVScanError) as ei:
        av_scan.scan_tree(tmp_path)
    assert "0x80004005" in str(ei.value)
    assert "MpCmdRun.log" in str(ei.value)
    assert "不是源目录检出威胁" in str(ei.value)


def test_defender_engine_failure_on_stderr(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    """故障行走 stderr 的同型分流 (stdout 无威胁名时尤其不能误报检出)。"""
    fake = _FakeRun(rc=2, stderr="CmdTool: Failed with hr = 0x80070005.\n")
    monkeypatch.setattr(av_scan, "defender_cmd_path", lambda: Path("C:/x/MpCmdRun.exe"))
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(av_scan.AVScanError, match="0x80070005"):
        av_scan.scan_tree(tmp_path)


def test_defender_missing_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(av_scan, "defender_cmd_path", lambda: None)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(av_scan, "clamscan_path", lambda: None)
    with pytest.raises(av_scan.AVUnavailable):
        av_scan.scan_tree(tmp_path)


def test_defender_error_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=1, stderr="bad args")
    monkeypatch.setattr(av_scan, "defender_cmd_path", lambda: Path("C:/x/MpCmdRun.exe"))
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(av_scan.AVScanError, match="退出码 1"):
        av_scan.scan_tree(tmp_path)


def test_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(raises=subprocess.TimeoutExpired(cmd="x", timeout=1))
    monkeypatch.setattr(av_scan, "defender_cmd_path", lambda: Path("C:/x/MpCmdRun.exe"))
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(av_scan.AVScanError, match="超时"):
        av_scan.scan_tree(tmp_path)


# ------------------------------------------------------------------ ClamAV

def test_clamav_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=0)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(av_scan, "clamscan_path", lambda: Path("/usr/local/bin/clamscan"))
    monkeypatch.setattr(subprocess, "run", fake)
    out = av_scan.scan_tree(tmp_path)
    assert out.clean
    assert "ClamAV" in out.engine


def test_clamav_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=1, stdout=f"{tmp_path}/a.txt: Eicar-Signature FOUND\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(av_scan, "clamscan_path", lambda: Path("/usr/local/bin/clamscan"))
    monkeypatch.setattr(subprocess, "run", fake)
    out = av_scan.scan_tree(tmp_path)
    assert out.threats == ["Eicar-Signature"]


def test_clamav_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(av_scan, "clamscan_path", lambda: None)
    with pytest.raises(av_scan.AVUnavailable, match="ClamAV"):
        av_scan.scan_tree(tmp_path)


# ------------------------------------------------------------------ 自定义

def test_custom_template_path_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=0)
    monkeypatch.setattr(subprocess, "run", fake)
    out = av_scan.scan_tree(tmp_path, av_cmd=f'"{tmp_path / "scan.exe"}" --deep --target {{path}}')
    assert out.clean
    assert fake.calls[0][0] == str(tmp_path / "scan.exe")
    assert "--target" in fake.calls[0]
    assert str(tmp_path) in fake.calls[0]
    assert "{path}" not in fake.calls[0]


def test_custom_threat_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=3, stdout="INFECTED a.exe\nINFECTED b.dll\n")
    monkeypatch.setattr(subprocess, "run", fake)
    out = av_scan.scan_tree(tmp_path, av_cmd="myav {path}")
    assert out.threats == ["INFECTED a.exe", "INFECTED b.dll"]
    assert out.engine == "myav"


def test_custom_priority_over_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """给了 av_cmd 就不再探测平台默认引擎。"""
    fake = _FakeRun(rc=0)
    monkeypatch.setattr(subprocess, "run", fake)

    def boom() -> Path | None:
        raise AssertionError("不应探测 Defender")
    monkeypatch.setattr(av_scan, "defender_cmd_path", boom)
    av_scan.scan_tree(tmp_path, av_cmd="myav {path}")


def test_custom_appends_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeRun(rc=0)
    monkeypatch.setattr(subprocess, "run", fake)
    av_scan.scan_tree(tmp_path, av_cmd="myav --quick")
    assert fake.calls[0][-1] == str(tmp_path)
