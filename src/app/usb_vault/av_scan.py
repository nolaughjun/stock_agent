"""烧录前病毒扫描 (设计文档 13.1): 明文阶段是内容唯一可扫时机。

引擎优先级: --av-cmd 自定义模板 > 平台默认 (Windows: Defender MpCmdRun;
其余平台: ClamAV)。

策略:
- fail-closed —— 无可用引擎即抛 AVUnavailable, 由调用方中止 (或 --skip-scan 豁免)
- 检测不处置 —— Defender 用 -DisableRemediation, 绝不让引擎删/隔离源文档
- 查出威胁返回清单, 调用方中止烧录
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT_S = 1800.0  # 大资料目录 Defender 全扫可能较慢

_MP_CMD_RELATIVE = Path("Windows Defender") / "MpCmdRun.exe"

_DEFENDER_THREAT_RE = re.compile(r"Threat\s+:\s*(\S.*)")
# 2026-09-17 真机: MpCmdRun 故障也吐退出码 2 ("CmdTool: Failed with hr =
# 0x80004005", av-scan 显示用时 0.0s —— 扫描根本没跑)。退出码 2 同时承载
# "检出威胁"和"工具故障"两种语义, 必须按输出分流, 否则引擎故障会被当成
# "源目录有毒", 把排障方向带偏。
_DEFENDER_FAIL_RE = re.compile(r"CmdTool:\s*Failed with hr\s*=\s*(0x[0-9A-Fa-f]+)")
_CLAM_FOUND_RE = re.compile(r"^(.+?):\s*(\S+)\s+FOUND$")


class AVUnavailable(Exception):
    """没有可用的病毒扫描引擎 (fail-closed: 默认中止, --skip-scan 显式豁免)。"""


class AVScanError(Exception):
    """引擎存在但执行失败 (超时 / 崩溃 / 未知退出码)。"""


@dataclass(slots=True)
class ScanOutcome:
    """一次扫描的结果; threats 为空即干净。"""

    engine: str
    duration_s: float
    threats: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.threats


# ---------------------------------------------------------------------------
# 引擎探测
# ---------------------------------------------------------------------------

def defender_cmd_path() -> Path | None:
    """定位 MpCmdRun.exe (Defender 随系统内置; 第三方 AV 顶替时可能不存在)。"""
    candidates = []
    pf = os.environ.get("ProgramFiles")
    pf86 = os.environ.get("ProgramFiles(x86)")
    if pf:
        candidates.append(Path(pf) / _MP_CMD_RELATIVE)
    if pf86:
        candidates.append(Path(pf86) / _MP_CMD_RELATIVE)
    candidates.append(Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe"))
    candidates.append(Path(r"C:\Program Files (x86)\Windows Defender\MpCmdRun.exe"))
    for p in candidates:
        if p.is_file():
            return p
    return None


def clamscan_path() -> Path | None:
    return Path(p) if (p := shutil.which("clamscan")) else None


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------

def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AVScanError(f"扫描超时 ({timeout:.0f}s): {argv[0]}") from exc
    except OSError as exc:
        raise AVScanError(f"扫描器无法执行: {argv[0]}: {exc}") from exc


def _scan_defender(root: Path, timeout: float) -> ScanOutcome:
    """MpCmdRun 自定义路径扫描; 退出码 0=干净 2=有威胁, 其余为执行错误。

    -DisableRemediation: 仅检测不处置, 不删不隔离源文件 (设计 13.1)。
    """
    cmd = defender_cmd_path()
    if cmd is None:
        raise AVUnavailable("未找到 Windows Defender 命令行 (MpCmdRun.exe)")
    start = time.monotonic()
    r = _run([str(cmd), "-Scan", "-ScanType", "3",
              "-File", str(root), "-DisableRemediation"], timeout=timeout)
    duration = time.monotonic() - start
    if r.returncode == 0:
        return ScanOutcome("Windows Defender", duration)
    if r.returncode == 2:
        fail = _DEFENDER_FAIL_RE.search(r.stdout) or _DEFENDER_FAIL_RE.search(r.stderr)
        if fail:
            log = Path(tempfile.gettempdir()) / "MpCmdRun.log"
            raise AVScanError(
                f"Defender 扫描器执行故障 (hr={fail[1]}), 未扫描任何文件 —— "
                f"是本机引擎问题, 不是源目录检出威胁; 详情见日志: {log} "
                "(常见: 被第三方杀软顶成被动模式, 看 Get-MpComputerStatus 的 "
                "AMRunningMode; 或 Defender 平台更新中途)")
        threats = _DEFENDER_THREAT_RE.findall(r.stdout)
        if not threats:
            threats = ["Defender 检测到威胁 (退出码 2), 输出未含威胁名; 请人工复核"]
        return ScanOutcome("Windows Defender", duration, threats)
    raise AVScanError(f"MpCmdRun 退出码 {r.returncode}: {r.stdout[-400:]} {r.stderr[-400:]}".strip())


def _scan_clamav(root: Path, timeout: float) -> ScanOutcome:
    """clamscan --infected --recursive; 0=干净 1=有威胁 2=错误。"""
    cmd = clamscan_path()
    if cmd is None:
        raise AVUnavailable("未找到 clamscan (macOS/Linux 烧录机请安装 ClamAV)")
    start = time.monotonic()
    r = _run([str(cmd), "--infected", "--recursive", str(root)], timeout=timeout)
    duration = time.monotonic() - start
    if r.returncode == 0:
        return ScanOutcome(f"ClamAV ({cmd.name})", duration)
    if r.returncode == 1:
        threats = [name for _path, name in _CLAM_FOUND_RE.findall(r.stdout)]
        if not threats:
            threats = ["ClamAV 检测到威胁 (退出码 1), 输出未含威胁名; 请人工复核"]
        return ScanOutcome(f"ClamAV ({cmd.name})", duration, threats)
    raise AVScanError(f"clamscan 退出码 {r.returncode}: {(r.stdout + r.stderr)[-400:]}".strip())


def _scan_custom(root: Path, template: str, timeout: float) -> ScanOutcome:
    """自定义引擎: 模板里 {path} 会被替换为目标路径; 非零退出视为有威胁。"""
    argv = [str(root) if tok == "{path}" else tok for tok in shlex.split(template)]
    if not any(str(root) == a for a in argv):
        argv.append(str(root))  # 模板没写 {path} 就追加, 保证扫得到目标
    engine = Path(argv[0]).name
    start = time.monotonic()
    r = _run(argv, timeout=timeout)
    duration = time.monotonic() - start
    if r.returncode == 0:
        return ScanOutcome(engine, duration)
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()][:5]
    return ScanOutcome(engine, duration, lines or [f"{engine} 退出码 {r.returncode} (视为有威胁)"])


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def scan_tree(root: Path, *, av_cmd: str | None = None,
              timeout: float = DEFAULT_TIMEOUT_S) -> ScanOutcome:
    """扫描目录; 干净返回 threats 为空的 ScanOutcome, 有威胁带回清单。

    AVUnavailable / AVScanError 由调用方决定如何呈现 (burn 一律中止)。
    """
    root = Path(root)
    if av_cmd is not None:
        return _scan_custom(root, av_cmd, timeout)
    if sys.platform == "win32":
        return _scan_defender(root, timeout)
    return _scan_clamav(root, timeout)
