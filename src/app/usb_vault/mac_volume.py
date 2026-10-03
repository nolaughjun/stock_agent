"""macOS 盘身份读取: diskutil 定位设备节点 → 读引导扇区 → 卷序列号。

仅 darwin 上可用 (设计文档 7.1); 测试通过 monkeypatch 注入, 不碰真硬件。

2026-09-15 真机实测的权限现实 (三通道):
1. /dev 直读 —— 设备节点属 root:operator 0640, 收件人账户恒 PermissionError,
   双击 .app 的 GUI 会话也不例外; 且卷挂载中打开块设备节点可能被以 EBUSY
   拒绝 (2026-09-17 真机: dd 报 Resource busy), 故读优先 raw 节点
   /dev/rdisk*, 打不开再退回块节点;
2. diskutil VolumeUUID —— 对 exFAT 实测不提供该键 (exFAT 无文件系统级 UUID,
   macOS 仅为 FAT/HFS+/APFS 合成), 通道保留但不可依赖;
3. 管理员授权直读 (admin_read_serial) —— osascript "with administrator
   privileges" 弹系统原生密码框, 收件人点一次授权, root 代读 512 字节后
   回 Python 解析 —— 查看器的最终兜底, 也是目前唯一可靠通道。

单值版 get_volume_serial_mac (烧录用) 保持宁死不猜; 查看端顺序:
直读 → UUID 候选 (GCM 裁决) → 管理员授权。
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from pathlib import Path

from src.app.usb_vault import boot_sector
from src.app.usb_vault.boot_sector import BootSectorError, BOOT_SECTOR_SIZE
from src.app.usb_vault.volume_id import VolumeIdError


def _device_identifier(volume_root: Path) -> str:
    """diskutil info <卷> → DeviceIdentifier (如 'disk4s1')。

    diskutil 拒识 (2026-09 真机案例: ls 看得到卷, diskutil 却返回非零) 时,
    退回解析 `mount` 输出反查设备节点 —— 两条路都不通才报错, 且把两条路
    的原始报错都带上, 便于现场定位 (盘没挂稳 / 路径不是卷根 / 非 exFAT)。
    """
    info, why = _diskutil_plist(volume_root.as_posix())
    if info is not None:
        ident = info.get("DeviceIdentifier")
        if ident:
            return str(ident)
    try:
        return _device_from_mount(volume_root)
    except VolumeIdError as exc:
        raise VolumeIdError(
            f"diskutil 不通 ({why or 'plist 无 DeviceIdentifier 键'}); {exc}"
        ) from exc


def _device_from_mount(volume_root: Path,
                       diskutil: subprocess.CompletedProcess[bytes] | None = None,
                       ) -> str:
    """`mount` 输出反查设备节点: 行形如 '/dev/disk4s1 on /Volumes/名 (exfat, ...)'。"""
    try:
        result = subprocess.run(["mount"], capture_output=True, check=False)
    except OSError as exc:
        raise VolumeIdError(f"无法运行 mount: {exc}") from exc
    want = volume_root.as_posix()  # mount 输出恒为 / 分隔; Windows 测试机也不跑偏
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        seg = line.split(" on ", 1)
        if len(seg) != 2 or not seg[0].startswith("/dev/"):
            continue
        if seg[1].split(" (", 1)[0] == want:
            return seg[0][len("/dev/"):]
    detail = ""
    if diskutil is not None and diskutil.stderr:
        detail = ("\n  diskutil 原始报错: "
                  + diskutil.stderr.decode("utf-8", "replace").strip()[:200])
    raise VolumeIdError(
        f"无法定位U盘设备节点 (未挂载/被拔出, 或路径不是卷根): {volume_root}{detail}\n"
        "  请核对:\n"
        "  1) --drive 必须是卷根挂载点本身, 如 --drive /Volumes/盘名 (不是其中子目录);\n"
        "  2) 盘名含空格时整个路径加引号: --drive \"/Volumes/My USB\";\n"
        "  3) 终端执行 diskutil list external 与 mount | grep /Volumes/ 看真实挂载名;\n"
        "  4) U盘须为 exFAT/FAT32/NTFS (磁盘工具.app 查看; NTFS 在 mac 为"
        "只读挂载, 查看器同样支持)。"
    )


def read_boot_sector(device: str) -> bytes:
    """读分区设备节点前 512 字节 (即卷引导扇区)。

    优先 raw 节点 (/dev/rdiskNsM): 2026-09-17 真机实测, 卷挂载中打开块
    设备节点会被内核以 EBUSY 拒绝 (dd 报 Resource busy), 而 raw 节点是
    macOS 原始 I/O 的规范接口, 对挂载卷只读允许; raw 打不开 (不存在等)
    再退回块节点, 两条路都不通才报错。
    """
    last_exc: OSError | None = None
    for node in (f"/dev/r{device}", f"/dev/{device}"):
        try:
            with open(node, "rb") as fh:
                return fh.read(BOOT_SECTOR_SIZE)
        except PermissionError as exc:
            # 2026-09-15 真机: GUI 会话直读 /dev 恒被拒, 重新插拔也无济于事;
            # 出路只有两条 —— 查看器内管理员授权流程 (app._mac_admin_serial),
            # 或命令行加 sudo 运行。raw 被拒则块节点同样会被拒, 不再重试。
            raise VolumeIdError(
                "读取U盘引导扇区权限不足: macOS 规定读取U盘硬件信息需要管理员"
                "权限 (命令行请用 sudo 运行; 双击打开的查看器会弹一次授权框)"
            ) from exc
        except OSError as exc:
            last_exc = exc
    raise VolumeIdError(
        f"读取设备节点失败 /dev/r{device} 与 /dev/{device}: {last_exc}"
    ) from last_exc


def get_volume_serial_mac(root: str | Path) -> int:
    """macOS 入口: /Volumes/<名> → 卷序列号 (与 Windows 端同值)。

    烧录端只准用本函数 (精确单值, 读不了就 sudo, 猜错半序 = Windows 端
    永远解不开); 查看端优先用 get_volume_serial_candidates_mac。
    """
    if sys.platform != "darwin":
        raise VolumeIdError("mac_volume 仅支持 macOS (Windows 走 GetVolumeInformationW)")
    ident = _device_identifier(Path(root))
    try:
        return boot_sector.parse_volume_serial(read_boot_sector(ident))
    except BootSectorError as exc:
        raise VolumeIdError(str(exc)) from exc


# ---------------------------------------------------------------------------
# 外置介质判据 (v2.4.8 拷离介质闸门, 2026-09-29)
# ---------------------------------------------------------------------------

def mount_point_of(path: str | Path) -> Path | None:
    """上溯路径找 /Volumes/<卷名> 挂载点; 不在其下 (桌面/内置盘等) 返回 None。

    与 viewer.app._locate_drive 的 darwin 分支同一走法 (设计 D2), 收敛成
    公共助手供两处使用。不做 resolve(): .app 内 sys.executable 本就是
    绝对真路径, resolve 反而可能把 firmlink 改写到别的名字下。
    """
    for parent in Path(path).parents:
        if parent.parent == Path("/Volumes"):
            return parent
    return None


_DISKUTIL_PLIST_FLAGS = ("-plist", "--plist")


def _diskutil_plist(target: str) -> tuple[dict[str, object] | None, str]:
    """diskutil info <flag> <目标> → (plist, 失败原因); 成功时原因为空串。

    **2026-10-01 真机实锤 (v2.4.10)**: 收件 Mac (macOS 26) 上
    `diskutil info --plist <目标>` 对任何目标 (含纯 ASCII 的 /dev/disk4)
    一律退出码 1 只打印用法说明, 而用法自述就是单横线 `[-plist]` —— 双横
    线写法被该版 diskutil 拒收, 与卷名/编码无关。v2.4.9 的级联三通道共用
    这一个写法 = 公共单点故障, 全灭。故本函数收敛为全模块唯一的 diskutil
    探测点, 按 (-plist, --plist) 双写法探测: 单横线是该机用法文本自载的
    形态且历来各版通用, 失败再补双横线 (旧机型的宽容写法), 两种都不通才
    返回 None (原因带两次尝试的退出码)。
    起不来 / 坏 plist 同样归 None —— 调用方 (介质判据级联 / UUID 通道 /
    设备定位) 各自记原因走下一通道, 不在此抛。
    """
    whys: list[str] = []
    for flag in _DISKUTIL_PLIST_FLAGS:
        try:
            result = subprocess.run(
                ["diskutil", "info", flag, target],
                capture_output=True, check=False,
            )
        except OSError as exc:
            return None, f"无法运行 diskutil ({exc})"
        if result.returncode == 0:
            try:
                return plistlib.loads(result.stdout), ""
            except plistlib.InvalidFileException as exc:
                return None, f"diskutil 输出不是合法 plist ({exc})"
        detail = result.stderr.decode("utf-8", "replace").strip()[:120]
        whys.append(f"{flag}: 退出码 {result.returncode} {detail or '无 stderr'}")
    return None, "diskutil " + "; ".join(whys)


def _external_verdict(info: dict[str, object]) -> bool | None:
    """diskutil plist → 外置与否; 可判的键都没有时返回 None (换下一通道)。

    Internal 键权威 (真USB棒恒 false); 2026-09-30 真机 (macOS 26, NTFS 走
    fskit 挂载) 实测卷级 plist 可能缺 Internal —— 同一支棒在文本 diskutil
    info 里明明白白 Device Location: External / Removable Media: Removable。
    DeviceLocation 与 Internal 同出 IOKit 盘属性 (文本输出的 Device Location
    行即由它渲染), 缺 Internal 时作同源旁证, 不引新事实源。
    """
    internal = info.get("Internal")
    if isinstance(internal, bool):
        return not internal
    location = info.get("DeviceLocation")
    if isinstance(location, str) and location.strip().lower() == "external":
        return True
    return None


def is_on_external_media(path: str | Path) -> bool:
    """路径是否位于外置介质挂载的卷上 (v2.4.8 拷离介质闸门判据)。

    两段判: ①上溯不到 /Volumes/<卷名> = 拷到内置盘/桌面的副本, 直接
    False; ②挂载点在 /Volumes 下也未必是U盘 —— 内置的额外数据卷同样
    挂 /Volumes (Catalina+ 数据卷就在其下), 须以 diskutil 的盘属性分辨。
    **2026-09-30 真机事故 (v2.4.9 修)**: macOS 26 的 NTFS 走 fskit 挂载,
    卷级 `diskutil info --plist` 可能缺 Internal 键 —— 旧实现单通道读不出
    即 VolumeIdError, 被 fail-closed 谢幕, 真棒上的阅览器被自家闸门错杀
    (弹出"脱离U盘介质")。改三级级联, 全灭才抛:
      1. 挂载点 plist: Internal 键 → 判; 缺 Internal 看 DeviceLocation
        (同源旁证);
      2. `mount` 输出反查设备节点 (复用 _device_from_mount, 得 disk4s1),
        对切片节点 /dev/disk4s1 再问 plist;
      3. 切片归整盘 (/dev/disk4) 问 Internal —— 整盘信息不经文件系统栈,
        fskit 卷缺失的键在这里仍在。
    diskutil 经 XPC 由 root 守护进程代劳, 自身免特权 (模块头注释通道 2
    同款)。全通道灭抛 VolumeIdError 且带上各通道原因 (现场截图即自诊断)
    —— 调用方 (app._exe_on_removable_media) 按 fail-closed 谢幕处理, 不猜。
    **2026-10-01 定谳修正 (v2.4.10)**: v2.4.9 当日真机复测推翻"plist 缺
    Internal 键"的机制推断 —— 该机 (macOS 26) diskutil 对双横线 --plist
    一律退出码 1 打用法说明 (selfcheck 三通道死因逐条带出, 全是 Usage),
    级联三通道共用该写法 = 公共单点故障。级联保留, 探测统一改
    _diskutil_plist 双写法 (-plist 优先) —— 级联的通道必须真正独立,
    否则再多通道也是同一个单点。
    """
    if sys.platform != "darwin":
        raise VolumeIdError("is_on_external_media 仅支持 macOS")
    mount = mount_point_of(path)
    if mount is None:
        return False
    notes: list[str] = []
    # as_posix 同 _device_from_mount: mac 上与 str() 等价, Windows 测试机不跑偏
    info, why = _diskutil_plist(mount.as_posix())
    if info is not None:
        verdict = _external_verdict(info)
        if verdict is not None:
            return verdict
        keys = ",".join(sorted(str(k) for k in info)[:8]) or "空 plist"
        notes.append(f"挂载点 plist 无 Internal/DeviceLocation 可判 (键: {keys})")
    else:
        notes.append(f"挂载点查询不通: {why}")
    # 通道 2/3: mount 反查设备节点 → 切片节点 → 整盘节点
    try:
        ident = _device_from_mount(mount)
    except VolumeIdError as exc:
        notes.append(f"mount 反查设备失败: {exc}")
        raise VolumeIdError("; ".join(notes)) from exc
    whole = re.sub(r"s\d+$", "", ident)
    for target in (f"/dev/{ident}", f"/dev/{whole}"):
        info, why = _diskutil_plist(target)
        if info is None:
            notes.append(f"{target} 查询不通: {why}")
            continue
        verdict = _external_verdict(info)
        if verdict is not None:
            return verdict
        notes.append(f"{target} plist 无 Internal/DeviceLocation 可判")
    raise VolumeIdError("; ".join(notes))


# ---------------------------------------------------------------------------
# 免特权降级通道: diskutil VolumeUUID (2026-09-15 真机: 对 exFAT 不提供)
# ---------------------------------------------------------------------------

_UUID_16x2 = re.compile(r"^([0-9A-Fa-f]{4})-([0-9A-Fa-f]{4})$")


def _diskutil_volume_uuid(volume_root: Path) -> tuple[str | None, str]:
    """diskutil info plist 里的 VolumeUUID (如 '40E8-75AA') + 失败原因说明。

    diskutil 经 XPC 让 root 守护进程代劳, 自身不需要任何特权; 探测走
    _diskutil_plist (2026-10-01 起双写法兜底, 见其 docstring)。返回
    (uuid, note): uuid 为 None 时 note 说明是哪一种失败 (启动不了 / 退出码
    非零 / plist 里没有该键) —— 2026-09-15 真机证明 exFAT 会落在第三种,
    把键名样本带回报错文案, 截图即自诊断, 不用再猜。
    """
    info, why = _diskutil_plist(volume_root.as_posix())
    if info is None:
        return None, why
    uuid_val = info.get("VolumeUUID")
    if uuid_val:
        return str(uuid_val), ""
    keys = ",".join(sorted(str(k) for k in info)[:12]) or "空 plist"
    return None, f"diskutil 信息里没有 VolumeUUID 键 (现有键: {keys})"


def uuid_serial_candidates(uuid: str | None) -> list[int]:
    """FAT/exFAT 的 VolumeUUID 由引导扇区序列号合成 (两个 16 位组)。

    两半先后次序无正式文档, 返回两种候选序; 真值由清单 GCM 认证唯一
    裁决 (错序列号 tag 必失败, 不存在碰撞面)。非 4-4 形态 (128 位全 UUID
    = 非序列号合成, 如 NTFS / 挂载时随机合成) 返回空表表示不可用。
    """
    if not uuid:
        return []
    m = _UUID_16x2.match(uuid.strip())
    if not m:
        return []
    hi, lo = int(m[1], 16), int(m[2], 16)
    cands = [(hi << 16) | lo, (lo << 16) | hi]
    return cands[:1] if cands[0] == cands[1] else cands


def get_volume_serial_candidates_mac(root: str | Path) -> list[int]:
    """macOS 查看端多候选入口: /dev 直读 (精确, 首选) 不通时走 UUID (免特权)。

    两个候选意味着最多两次 scrypt+GCM 尝试 (仅降级路径, 直读成功仍单次);
    免特权通道也不可用时抛 VolumeIdError, 文案带直读原因与 UUID 通道的
    具体失败方式 (供现场截图定位; 调用方可再走管理员授权通道兜底)。
    """
    if sys.platform != "darwin":
        raise VolumeIdError("mac_volume 仅支持 macOS (Windows 走 GetVolumeInformationW)")
    try:
        return [get_volume_serial_mac(root)]
    except VolumeIdError as exc:
        uuid, note = _diskutil_volume_uuid(Path(root))
        cands = uuid_serial_candidates(uuid)
        if cands:
            return cands
        if uuid:
            detail = f"免特权通道 VolumeUUID={uuid} 非序列号合成形态, 不可用"
        else:
            detail = f"免特权通道不可用: {note}"
        raise VolumeIdError(f"{exc}; {detail}") from exc


# ---------------------------------------------------------------------------
# 管理员授权直读 (sudo -S 载体): 当前唯一可靠通道
# 2026-09-15 三通道排查结论"需要管理员"; 2026-09-18 四连探针判死 osascript
# 载体 (/dev 全面 EPERM), 改与终端同机制的 sudo —— 在真机上被验证可读。
# ---------------------------------------------------------------------------

_SAFE_IDENT = re.compile(r"^[0-9A-Za-z]+$")


def _admin_note(reason: str) -> None:
    """授权通道诊断落 stderr: 终端启动的查看器把原因直接打在终端里。

    双击启动的 --windowed 产物没有控制台, sys.stderr 可能为 None ——
    噤声跳过 (不改变任何行为, 只丢诊断), 绝不因打印本身报错。
    """
    if sys.stderr is not None:
        print(f"[admin_read_serial] {reason}", file=sys.stderr)


class AdminAuthRejected(Exception):
    """sudo 拒绝了密码 (输错/无 sudo 权限) —— 调用方可重新询问后再试。"""


def admin_read_serial(root: str | Path, password: str) -> int | None:
    """sudo -S 管理员授权读序列号: 查看器自弹密码框 → 与终端同机制代读。

    2026-09-18 四连探针判死 osascript 载体: `do shell script with
    administrator privileges` 的 root shell 对 /dev 全面 EPERM (挂载/
    已卸载 × slice/整盘节点共四种组合全拒), 而同一块盘在终端 `sudo dd`
    下可读 —— 是载体之差, 非 root 之差。故管理员通道改走 sudo -S:
    密码经 stdin 喂给 sudo (不进 argv, ps 里看不到), root shell 内
    dd raw 节点优先/块节点回退, od 转十六进制, 输出自述 DD_RC/DD_ERR。

    设备标识符来自 diskutil, 白名单校验 (^[0-9A-Za-z]+$) 后才拼 shell
    —— 注入面归零。密码不对/无 sudo 权限 → AdminAuthRejected (调用方
    决定重试); 其余失败 → None 且原图落 stderr (终端启动可见)。
    sudo 后仍 EPERM 的场合, stderr 附"完全磁盘访问权限"指引 (部分
    macOS 版本把 /dev 原始读挂在 TCC 门后)。
    """
    if sys.platform != "darwin":
        return None
    try:
        ident = _device_identifier(Path(root))
    except VolumeIdError as exc:
        _admin_note(f"diskutil 定位设备失败: {exc}")
        return None
    if not _SAFE_IDENT.fullmatch(ident):
        _admin_note(f"设备标识符未过白名单, 拒绝拼 shell: {ident!r}")
        return None
    # 单次授权内完成读+自述: dd 输出与错误落 mktemp 临时文件 (root 私有),
    # od 转十六进制后追加 DD_RC=/DD_ERR= 两行 —— Python 侧据此分清
    # "读到了但解析失败"与"root 下 dd 本身失败(错误原文一并带回)"。
    inner = (
        'out=$(mktemp /tmp/usbvault.out.XXXXXX); '
        'err=$(mktemp /tmp/usbvault.err.XXXXXX); '
        f'( dd if=/dev/r{ident} bs=512 count=1 2>$err '
        f'|| dd if=/dev/{ident} bs=512 count=1 2>$err ) > $out; '
        'rc=$?; od -An -tx1 -v < $out; '
        'echo DD_RC=$rc; echo DD_ERR=$(cat $err); rm -f $out $err'
    )
    try:
        result = subprocess.run(
            ["sudo", "-S", "-p", "", "/bin/sh", "-c", inner],
            input=(password + "\n").encode(),
            capture_output=True, check=False)
    except OSError as exc:
        _admin_note(f"sudo 无法启动: {exc}")
        return None
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        if ("password" in err or "not in the sudoers" in err
                or "try again" in err):
            raise AdminAuthRejected(err[:200]) from None
        _admin_note(f"sudo 退出码 {result.returncode}: {err[:200]}")
        return None
    dd_rc, dd_err = "0", ""
    hex_lines: list[str] = []
    for ln in result.stdout.decode("utf-8", "replace").splitlines():
        if ln.startswith("DD_RC="):
            dd_rc = ln[len("DD_RC="):].strip() or "0"
        elif ln.startswith("DD_ERR="):
            dd_err = ln[len("DD_ERR="):].strip()
        else:
            hex_lines.append(ln)
    if dd_rc != "0":
        hint = ""
        if "not permitted" in dd_err:
            hint = (" —— 若已授权仍如此: 系统设置→隐私与安全性→完全磁盘访问"
                    "权限, 给图书阅览器.app 打开开关后重试")
        _admin_note(f"root 下 dd 仍失败 (退出码 {dd_rc}): "
                    f"{dd_err or '无错误输出'}{hint}")
        return None
    try:
        sector = bytes.fromhex("".join(" ".join(hex_lines).split()))
        return boot_sector.parse_volume_serial(sector)
    except (ValueError, BootSectorError) as exc:
        _admin_note(f"授权读到了数据但解析失败: {exc}")
        return None
