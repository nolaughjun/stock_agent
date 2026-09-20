"""查看器自检 (--selfcheck): 一条命令体检产物健康度。

2026-09-18 真机循环的教训: 多机手工传送链 (代码拷贝→构建→拷盘→换机) 任何
一环拿到旧部件都会在收件人端爆炸, 而产物自己无法证明新旧 —— rdisk 修复在
代码里躺了两天始终没真正到达任何一张盘, 靠现象反推了三轮。冻结的查看器带上
本入口后, 构建机出炉直跑、盘上产物抽查都只须一条命令:

    图书阅览器.app/Contents/MacOS/vault_viewer --selfcheck

打印: 构建版本 / tkinter / 内嵌 SEED / 数据卷定位 / 序列号通道状态;
全 ✔ 退出码 0, 任一 ✗ 退出码 1。不弹 GUI、不触发管理员授权框、不解密,
收件人侧与构建侧通用。配合窗口标题的版本号 (v2.3.3), 旧产物一眼可辨。
"""

from __future__ import annotations

import sys
from pathlib import Path

from src.app.usb_vault import config


def run_selfcheck(own: Path | None = None) -> int:
    """体检当前查看器产物; 返回 0=全绿 / 1=有问题。own 缺省=可执行文件所在目录。"""
    ok = True

    def line(label: str, detail: str = "", *, bad: bool = False) -> None:
        nonlocal ok
        if bad:
            ok = False
        print(f"[{'✗' if bad else '✔'}] {label}" + (f": {detail}" if detail else ""))

    line("构建版本", config.VIEWER_VERSION)
    try:
        import tkinter
        line("tkinter", f"Tcl/Tk {tkinter.TkVersion}")
    except ImportError as exc:
        line("tkinter 缺失 (产物一启动就闪退)", str(exc), bad=True)

    try:
        from src.app.usb_vault.seed import load_embedded_seed
        seed = load_embedded_seed()
        line("内嵌 SEED", f"{len(seed)} 字节")
    except Exception as exc:  # noqa: BLE001 -- 自检的职责就是让任何失败显形
        line("内嵌 SEED 读取失败", str(exc), bad=True)

    if own is None:
        own = Path(sys.executable).resolve().parent
    if sys.platform == "darwin":
        from src.app.usb_vault.mac_volume import (
            _diskutil_volume_uuid,
            get_volume_serial_mac,
            uuid_serial_candidates,
        )
        from src.app.usb_vault.volume_id import VolumeIdError
        from src.app.usb_vault.viewer.app import _mac_data_volume_candidates

        cands = _mac_data_volume_candidates(own)
        if not cands:
            line("数据卷未找到", f"{own} 与 /Volumes 兄弟卷均无 keyinfo", bad=True)
        for cand in cands:
            detail = str(cand)
            try:
                serial = get_volume_serial_mac(cand)
                detail += f"  序列号 0x{serial:08X} (直读)"
            except VolumeIdError:
                uuid, note = _diskutil_volume_uuid(cand)
                if uuid_serial_candidates(uuid):
                    detail += f"  免特权 UUID 候选可用 ({uuid})"
                else:
                    detail += "  直读/UUID 不通 — 双击流程走授权框兜底"
            line("数据卷", detail)
    else:
        line("数据卷检查", "仅 macOS 布点 (Windows 无 /dev 权限墙)")

    print("自检" + ("通过" if ok else "未通过"))
    return 0 if ok else 1
