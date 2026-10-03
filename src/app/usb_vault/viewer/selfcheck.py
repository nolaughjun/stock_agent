"""查看器自检 (--selfcheck): 一条命令体检产物健康度。

2026-09-18 真机循环的教训: 多机手工传送链 (代码拷贝→构建→拷盘→换机) 任何
一环拿到旧部件都会在收件人端爆炸, 而产物自己无法证明新旧 —— rdisk 修复在
代码里躺了两天始终没真正到达任何一张盘, 靠现象反推了三轮。冻结的查看器带上
本入口后, 构建机出炉直跑、盘上产物抽查都只须一条命令:

    图书阅览器.app/Contents/MacOS/vault_viewer --selfcheck

打印: 构建版本 / 自身介质 (v2.4.8 闸门预演) / tkinter / 内嵌 SEED /
数据卷定位 / 序列号通道状态;
全 ✔ 退出码 0, 任一 ✗ 退出码 1。不弹 GUI、不触发管理员授权框、不解密,
收件人侧与构建侧通用。配合窗口标题的版本号 (v2.4.0), 旧产物一眼可辨。
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
    # v2.4.8 自身介质闸门预演 (镜像 _startup_gate 顺序, 介质检查在最先):
    # 冻结构建不在可移动介质上 = 启动门将谢幕, 自检直接 ✗; 同时服务换棒
    # 红线 —— 真棒插上跑一次自检, 介质类型在这里直接报出 (个别 USB-SSD
    # 类棒报 fixed, 会被硬闸误伤, 换棒型先看这一行)。
    try:
        from src.app.usb_vault.viewer.app import _exe_on_removable_media

        if not getattr(sys, "frozen", False):
            line("自身介质", "源码运行 (烧录机预览, 介质闸门不启用)")
        elif _exe_on_removable_media():
            line("自身介质", "可移动介质 (闸门放行)")
        else:
            detail = ""
            if sys.platform == "win32":
                try:
                    from src.app.usb_vault.volume_id import get_drive_type

                    detail = f"GetDriveType={get_drive_type(sys.executable)}, "
                except Exception as exc:  # noqa: BLE001 -- 自检的职责就是让失败显形
                    detail = f"类型读取失败 ({exc}), "
            elif sys.platform == "darwin":
                from src.app.usb_vault.mac_volume import (
                    is_on_external_media,
                    mount_point_of,
                )
                from src.app.usb_vault.volume_id import VolumeIdError

                mount = mount_point_of(sys.executable)
                detail = f"挂载点={mount or '不在 /Volumes 下'}, "
                if mount is not None:
                    # v2.4.9: 复跑判据把级联各通道原因带出 —— 真机再遇
                    # 误谢幕, 这一行直接给出 diskutil 哪个键读不出。
                    try:
                        is_on_external_media(sys.executable)
                        detail += "判据=内置盘, "
                    except VolumeIdError as exc:
                        detail += f"判据全通道不通 ({exc}), "
            line("自身介质", f"{detail}冻构建不在可移动介质 (或判据读不出), 启动将谢幕",
                 bad=True)
    except Exception as exc:  # noqa: BLE001 -- 自检的职责就是让任何失败显形
        line("自身介质判据异常", str(exc), bad=True)
    try:
        import tkinter

        line("tkinter", f"Tcl/Tk {tkinter.TkVersion}")
    except ImportError as exc:
        line("tkinter 缺失 (产物一启动就闪退)", str(exc), bad=True)

    if sys.platform == "darwin":
        # v2.4.11 防截屏链核验: v2.3.3 (Win) 与 v2.4.11 前 (Mac) 都是
        # "静默失效无人知" —— 现在构建机出炉一条命令即知防护是否在岗。
        # 隐藏探测窗未映射进 NSApp.windows 不算失败 (露窗后常驻重刷
        # 自动纳入); 拿到窗口而回读≠None 才 ✗。
        from src.app.usb_vault.viewer import mac_protection

        try:
            probe = tkinter.Tk()
            probe.withdraw()
            probe.update()
            status, detail = mac_protection.protect_all_windows()
            if status == "ok":
                line("防截屏", detail)
            elif status == "no_windows":
                line("防截屏", "隐藏探测窗未入列 (露窗后自动纳入, 微信实测为准)")
            else:  # fail / no_objc
                line("防截屏", detail, bad=True)
            probe.destroy()
        except Exception as exc:  # noqa: BLE001 自检的职责就是让失败显形
            line("防截屏", f"无法核验 ({exc})")

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
        # v2.4.0 反转布局: 加密链在无盘符分区2, Windows 侧也能 (且需要) 找卷。
        # 构建机出炉自检时通常没插盘 —— 无命中不算失败, 只列明候选状态;
        # 盘上抽查时无命中则说明加密链定位链路断了, 按盘上排查处理。
        from src.app.usb_vault.volume_id import VolumeIdError, get_volume_serial
        from src.app.usb_vault.win_volumes import data_volume_candidates

        cands = data_volume_candidates(own)
        if not cands:
            line("数据卷检查", "无 keyinfo 命中卷 (未插盘/烧录机环境属正常)")
        for cand in cands:
            detail = str(cand)
            try:
                serial = get_volume_serial(cand)
                detail += f"  序列号 0x{serial:08X}"
            except VolumeIdError:
                detail += "  序列号直读不通"
            line("数据卷", detail)

    print("自检" + ("通过" if ok else "未通过"))
    return 0 if ok else 1
