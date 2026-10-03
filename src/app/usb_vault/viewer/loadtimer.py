"""装载计时探针 (Phase 0, 2026-09-24): 环境变量门控的分段计时, 归因大书慢在哪。

用法: 设 USBVAULT_LOAD_TIMER=1 后启动阅览器, 每次打开书在悬浮"装载计时"窗
追加分段耗时行; 不设该变量 = 零行为差异 (各调用点只多一次 enabled() 判断,
不进任何计时分支) —— 与无探针产物的收件人可见行为逐项相同, 不升版本号。

线程契约: log() 只在主线程调用 (装载/构造本就在主线程; 首帧的栅格计时在
工作线程测, 经闭包携带、apply 落地时才回主线程 log) —— tkinter 跨线程
不安全, sink 回调由 ViewerApp 在主线程注册。
"""

from __future__ import annotations

import os
import sys
import time
from typing import BinaryIO, Callable

ENV_NAME = "USBVAULT_LOAD_TIMER"


def enabled() -> bool:
    """探针开关: 未设/设 0 = 关 (默认), 其余任何值 = 开。"""
    return os.environ.get(ENV_NAME, "") not in ("", "0")


# 探针窗追加回调 (主线程注册; None = 尚无窗口, 仅内存留档 + stderr)
_sink: Callable[[str], None] | None = None
_lines: list[str] = []


def set_sink(sink: Callable[[str], None] | None) -> None:
    """注册/注销探针窗的追加回调 (ViewerApp, 主线程)。"""
    global _sink
    _sink = sink


def history() -> list[str]:
    """已累积的探针行副本 (探针窗 (重)建时回放, 不丢历史)。"""
    return list(_lines)


def log(text: str) -> None:
    """追加一行探针输出: 内存留档 + stderr (dev 终端) + sink (探针窗)。"""
    _lines.append(text)
    try:
        print(f"[loadtimer] {text}", file=sys.stderr)
    except (OSError, ValueError):
        pass  # --windowed 产物 stderr=None: 探针窗是唯一出口, 不让探针炸流程
    if _sink is not None:
        _sink(text)


class StageTimer:
    """顺序分段计时: begin() 起点, mark(段名) 收段, line() 拼一行汇总。

    只做"名 X.XXs | ..."的拼接, 不碰 Tk —— 可单测 (时钟可 monkeypatch)。
    """

    def __init__(self) -> None:
        self._t0: float | None = None
        self._marks: list[tuple[str, float]] = []

    def begin(self) -> None:
        self._t0 = time.perf_counter()
        self._marks.clear()

    def mark(self, name: str) -> None:
        self._marks.append((name, time.perf_counter()))

    def line(self) -> str:
        """少于两个时间点 = 没有可计段, 返回空串 (调用方自便)。"""
        if self._t0 is None or not self._marks:
            return ""
        parts = [
            f"{name} {t - prev:.2f}s"
            for (name, t), prev in zip(
                self._marks, [self._t0, *(t for _, t in self._marks[:-1])]
            )
        ]
        parts.append(f"合计 {self._marks[-1][1] - self._t0:.2f}s")
        return " | ".join(parts)


class TimedReader:
    """把 read 耗时/字节数从"读+解密"总时间里拆出来的只读包装。

    decrypt_chunks 只依赖 read(n) —— 包装层零侵入 crypto.py; 解密耗时
    ≈ 装载循环总时长 - read_s (含 bytearray += 的 Python 开销, 探针行里
    记作"解密+拼接", 不冒充纯解密)。上下文协议转交内层句柄。
    """

    def __init__(self, src: BinaryIO) -> None:
        self._src = src
        self.read_s = 0.0
        self.read_bytes = 0

    def read(self, n: int = -1) -> bytes:
        t0 = time.perf_counter()
        data = self._src.read(n)
        self.read_s += time.perf_counter() - t0
        self.read_bytes += len(data)
        return data

    def mbps(self) -> float:
        """顺序读吞吐 (MiB/s); 没读过 = 0.0 (除零防)。"""
        return self.read_bytes / 1048576 / self.read_s if self.read_s > 0 else 0.0

    def __enter__(self) -> TimedReader:
        self._src.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._src.__exit__(exc_type, exc, tb)  # type: ignore[arg-type]
