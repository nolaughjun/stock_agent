"""P2 深度取证适配器

对齐 02_phase2_design.md §4
- ZeekDeepAdapter   : 流量深度分析(C2信标/DGA/DNS隧道), ES DSL
- VelociraptorAdapter: 终端取证(Artifacts), REST
- YARAAdapter       : 恶意代码规则匹配, yara-python SDK
- CAPEAdapter       : 沙箱样本分析, REST 异步轮询
- VolatilityAdapter : 内存取证, 命令行插件

均支持 mock 降级(无真实服务配置时返回 canned 深度证据, §13 外部依赖对策)。
CAPE/Volatility 耗时, collect 内部带超时, 不阻塞主流程。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from src.app.traceback.adapters.base import BaseAdapter
from src.app.traceback.adapters.mock_data import get_deep_mock_evidence
from src.app.traceback.core.config import settings
from src.app.traceback.core.logging import logger
from src.app.traceback.schemas.evidence import CollectContext, RawEvidence


class ZeekDeepAdapter(BaseAdapter):
    """流量深度分析 (Zeek→ES)。区别于 SIEM 基础查询, 提取 C2 信标/DGA/DNS 隧道。"""

    name = "zeek"

    def __init__(self) -> None:
        self.es_hosts = settings.zeek_es_hosts
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> bool:
        if self.es_hosts:
            self._client = httpx.AsyncClient(
                base_url=self.es_hosts.split(",")[0], timeout=settings.evidence_collect_timeout
            )
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        if not self._client:
            return get_deep_mock_evidence("zeek", ctx)
        # 真实: ES DSL 查 zeek-conn/dns/http 索引
        raise NotImplementedError("真实 Zeek ES 查询待对接")


class VelociraptorAdapter(BaseAdapter):
    """终端取证 (Velociraptor Artifacts, REST)。"""

    name = "velociraptor"

    def __init__(self) -> None:
        self.url = settings.velociraptor_url
        self.api_key = settings.velociraptor_api_key

    async def connect(self) -> bool:
        return True  # mock 始终可用

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        if not self.url:
            return get_deep_mock_evidence("velociraptor", ctx)
        # 真实: collect_artifact 创建 hunt → 轮询(最长5min) → 解析
        raise NotImplementedError("真实 Velociraptor 对接待对接")


class YARAAdapter(BaseAdapter):
    """恶意代码规则匹配 (yara-python SDK)。同步快扫。"""

    name = "yara"

    def __init__(self) -> None:
        self.rules_dir = settings.yara_rules_dir
        self._rules: Any = None

    async def connect(self) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        if not self.rules_dir:
            return get_deep_mock_evidence("yara", ctx)
        # 真实: 加载 rules_dir 规则, 扫描证据文件
        if self._rules is None:
            try:
                import yara  # type: ignore

                def _compile():
                    files = {}
                    import os
                    for root, _, fnames in os.walk(self.rules_dir):
                        for f in fnames:
                            if f.endswith((".yar", ".yara")):
                                files[f] = os.path.join(root, f)
                    return yara.compile(filepaths=files) if files else None

                self._rules = await asyncio.to_thread(_compile)
            except Exception as e:  # noqa: BLE001
                logger.warning("YARA 规则加载失败, 降级 mock: {}", str(e)[:100])
                return get_deep_mock_evidence("yara", ctx)
        # 真实扫描逻辑待对接 (需证据文件路径)
        raise NotImplementedError("真实 YARA 扫描待对接(需证据文件路径)")


class CAPEAdapter(BaseAdapter):
    """沙箱样本分析 (CAPE, REST 异步)。耗时 5~15min, collect 带超时降级。"""

    name = "cape"

    def __init__(self) -> None:
        self.url = settings.cape_url
        self.api_key = settings.cape_api_key
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> bool:
        if self.url:
            self._client = httpx.AsyncClient(
                base_url=self.url,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                timeout=30,
            )
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        if not self._client:
            return get_deep_mock_evidence("cape", ctx)
        # 真实: 提交样本 → 轮询任务状态(超时降级)
        try:
            return await asyncio.wait_for(
                self._submit_and_poll(ctx), timeout=settings.evidence_collect_timeout * 5
            )
        except asyncio.TimeoutError:
            logger.warning("CAPE 分析超时, 降级 mock")
            return get_deep_mock_evidence("cape", ctx)

    async def _submit_and_poll(self, ctx: CollectContext) -> list[RawEvidence]:
        raise NotImplementedError("真实 CAPE 提交+轮询待对接")


class VolatilityAdapter(BaseAdapter):
    """内存取证 (Volatility3 命令行插件)。GB 级镜像耗时, collect 带超时降级。"""

    name = "volatility"

    def __init__(self) -> None:
        self.vol_path = settings.volatility_path

    async def connect(self) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def collect(self, ctx: CollectContext) -> list[RawEvidence]:
        if not self.vol_path:
            return get_deep_mock_evidence("volatility", ctx)
        # 真实: 子进程调用 volatility3 插件, 超时降级
        try:
            return await asyncio.wait_for(
                self._run_plugins(ctx), timeout=settings.evidence_collect_timeout * 5
            )
        except asyncio.TimeoutError:
            logger.warning("Volatility 分析超时, 降级 mock")
            return get_deep_mock_evidence("volatility", ctx)

    async def _run_plugins(self, ctx: CollectContext) -> list[RawEvidence]:
        raise NotImplementedError("真实 Volatility 插件调用待对接(需内存镜像路径)")


def deep_adapters() -> dict[str, BaseAdapter]:
    """P2 深度取证 5 适配器"""
    return {
        "zeek": ZeekDeepAdapter(),
        "velociraptor": VelociraptorAdapter(),
        "yara": YARAAdapter(),
        "cape": CAPEAdapter(),
        "volatility": VolatilityAdapter(),
    }
