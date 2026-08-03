"""适配器注册工厂

对齐 01_phase1_design.md §7 — P1 注册 5 个适配器。
应用启动时调用 register_defaults(manager), manager.initialize() 并行连接+健康检查。
"""
from __future__ import annotations

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.adapters.deep_adapters import deep_adapters
from src.app.traceback.adapters.dns_adapter import DNSAdapter
from src.app.traceback.adapters.edr_adapter import EDRAdapter
from src.app.traceback.adapters.manager import ComponentManager
from src.app.traceback.adapters.misp_adapter import MISPAdapter
from src.app.traceback.adapters.opencti_adapter import OpenCTIAdapter
from src.app.traceback.adapters.siem_adapter import SIEMAdapter
from src.app.traceback.adapters.timesketch_adapter import TimesketchAdapter


def default_adapters() -> dict[str, BaseAdapter]:
    """P1 证据源 + P2 归因后端 + P2 深度取证"""
    adapters: dict[str, BaseAdapter] = {
        "siem": SIEMAdapter(),
        "edr": EDRAdapter(),
        "dns": DNSAdapter(),
        "misp": MISPAdapter(),
        "timesketch": TimesketchAdapter(),
        "opencti": OpenCTIAdapter(),
    }
    adapters.update(deep_adapters())
    return adapters


def register_defaults(manager: ComponentManager) -> None:
    for name, adapter in default_adapters().items():
        manager.register(name, adapter)
