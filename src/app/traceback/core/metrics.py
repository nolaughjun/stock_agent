"""轻量 Prometheus 指标 (OBS-002, 无外部依赖)

对齐 04_phase4_design.md §4.3
进程内 Counter/Gauge/Histogram + Prometheus 文本 exposition (/metrics 端点)。
生产可换 prometheus_client 或 PrometheusPushGateway; 接口一致。
"""
from __future__ import annotations

import threading
import time
from typing import Any

# 默认 histogram 桶(秒): 适配阶段耗时(秒级~分钟级)
_DEFAULT_BUCKETS = (1, 5, 10, 30, 60, 180, 300, 600, 1800)

_lock = threading.Lock()
_registry: dict[str, Any] = {}


class Counter:
    def __init__(self, name: str, desc: str, labels: tuple[str, ...] = ()):
        self.name = name
        self.desc = desc
        self.label_names = labels
        self._values: dict[tuple, float] = {}

    def inc(self, amount: float = 1.0, **labels) -> None:
        key = tuple(labels.get(n, "") for n in self.label_names)
        with _lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def expose(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.desc}", f"# TYPE {self.name} counter"]
        if not self._values:
            lines.append(f'{self.name} 0')
            return lines
        for key, val in sorted(self._values.items()):
            label_str = _format_labels(self.label_names, key)
            lines.append(f"{self.name}{label_str} {val}")
        return lines


class Gauge:
    def __init__(self, name: str, desc: str, labels: tuple[str, ...] = ()):
        self.name = name
        self.desc = desc
        self.label_names = labels
        self._values: dict[tuple, float] = {}

    def set(self, value: float, **labels) -> None:
        key = tuple(labels.get(n, "") for n in self.label_names)
        with _lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels) -> None:
        key = tuple(labels.get(n, "") for n in self.label_names)
        with _lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels) -> None:
        self.inc(-amount, **labels)

    def expose(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.desc}", f"# TYPE {self.name} gauge"]
        if not self._values:
            lines.append(f"{self.name} 0")
            return lines
        for key, val in sorted(self._values.items()):
            label_str = _format_labels(self.label_names, key)
            lines.append(f"{self.name}{label_str} {val}")
        return lines


class Histogram:
    def __init__(self, name: str, desc: str, buckets: tuple = _DEFAULT_BUCKETS, labels: tuple[str, ...] = ()):
        self.name = name
        self.desc = desc
        self.buckets = buckets
        self.label_names = labels
        self._counts: dict[tuple, list[int]] = {}  # 每标签: bucket 计数
        self._sums: dict[tuple, float] = {}
        self._totals: dict[tuple, int] = {}

    def observe(self, value: float, **labels) -> None:
        key = tuple(labels.get(n, "") for n in self.label_names)
        with _lock:
            if key not in self._counts:
                self._counts[key] = [0] * len(self.buckets)
                self._sums[key] = 0.0
                self._totals[key] = 0
            for i, b in enumerate(self.buckets):
                if value <= b:
                    self._counts[key][i] += 1
            self._sums[key] += value
            self._totals[key] += 1

    def expose(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.desc}", f"# TYPE {self.name} histogram"]
        for key in sorted(self._counts):
            label_base = _format_labels(self.label_names, key, exclude_empty=True)
            prefix = f"{label_base}," if label_base else ""
            for i, b in enumerate(self.buckets):
                lines.append(f'{self.name}_bucket({prefix}le="{b}") {self._counts[key][i]}')
            lines.append(f'{self.name}_bucket({prefix}le="+Inf") {self._totals[key]}')
            lines.append(f"{self.name}_sum{label_base} {self._sums[key]}")
            lines.append(f"{self.name}_count{label_base} {self._totals[key]}")
        return lines


def _format_labels(names: tuple[str, ...], values: tuple, exclude_empty: bool = False) -> str:
    pairs = [(n, v) for n, v in zip(names, values) if not (exclude_empty and v == "")]
    if not pairs:
        return ""
    return "{" + ",".join(f'{n}="{v}"' for n, v in pairs) + "}"


# ---- 业务指标实例 (对齐 §4.3) ----
tasks_total = Counter("traceback_tasks_total", "任务计数", labels=("tenant", "stage"))
stage_duration = Histogram("traceback_stage_duration_seconds", "各阶段耗时(秒)", labels=("stage",))
active_tasks = Gauge("traceback_active_tasks", "当前并发任务数", labels=("tenant",))
llm_calls_total = Counter("traceback_llm_calls_total", "LLM 调用计数", labels=("agent", "status"))
adapter_errors = Counter("traceback_adapter_errors", "适配器错误计数", labels=("source",))
knowledge_hits = Counter("traceback_knowledge_hits", "知识推荐命中计数")

_ALL = [tasks_total, stage_duration, active_tasks, llm_calls_total, adapter_errors, knowledge_hits]


def render_metrics() -> str:
    """渲染全部指标为 Prometheus 文本格式"""
    lines: list[str] = []
    for m in _ALL:
        lines.extend(m.expose())
    return "\n".join(lines) + "\n"


def observe_stage_time(stage: str) -> "_StageTimer":
    """阶段耗时上下文管理器: with observe_stage_time('collecting'): ..."""
    return _StageTimer(stage)


class _StageTimer:
    def __init__(self, stage: str):
        self.stage = stage
        self._start = 0.0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        stage_duration.observe(time.monotonic() - self._start, stage=self.stage)
        return False
