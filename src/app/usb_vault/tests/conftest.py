"""tests 共享夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.app.usb_vault import config


@pytest.fixture()
def dist_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离开发机真实 dist/ 产物 (烧录类测试按文件 usefixtures 选用)。

    不传 viewer_mac 的 burn 原会捡到开发机 dist/ 里的真 .app —— 其内嵌
    SEED 与测试随机 SEED 必然不一致 → 内嵌 SEED 硬拦触发 (2026-09 加入的
    正确生产行为, 测试要的却是确定性)。顺带阻断"自动构建"分支, 防止测试
    里真跑 PyInstaller。不能设为 autouse: standalone e2e 等用例需要真实
    config.MODULE_DIR。"""
    monkeypatch.setattr(config, "MODULE_DIR", tmp_path / "isolated")
    monkeypatch.setitem(sys.modules, "src.app.usb_vault.viewer.build_viewer", None)
    # 同会话真导入过 build_viewer 时 (全量跑 test_build_viewer 在前), 包对象上
    # 挂着同名属性, `from ...viewer import build_viewer` 走属性捷径绕过
    # sys.modules 的 None 桩 => 隔离失效、自动构建被触发 (单跑不复现)。
    # 连包属性一起剥掉才是完整隔离。
    pkg = sys.modules.get("src.app.usb_vault.viewer")
    if pkg is not None and hasattr(pkg, "build_viewer"):
        monkeypatch.delattr(pkg, "build_viewer")


@pytest.fixture(autouse=True)
def code_archive_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """解锁码归档隔离 (autouse): 密码模式烧录 _archive_code 在没传
    code_archive 时落到 config.CODE_ARCHIVE_PATH —— 真实 secrets/ 归档。
    2026-09-20 实测教训: pw_drive/_burn_volume 等用例漏传, 门禁每跑一轮
    往正式归档写数百条测试码 (序列号 0x11223344), 交付前清理形同泡影。
    测试一律写 tmp, 乙方归档只进正式烧录。
    """
    monkeypatch.setattr(config, "CODE_ARCHIVE_PATH",
                        tmp_path / "unlock_codes_test.jsonl")
