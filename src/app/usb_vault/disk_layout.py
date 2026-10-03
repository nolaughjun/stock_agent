"""双入口分区的磁盘布局服务层 (--dual-entry 烧录编排的底层)。

仅 Windows 烧录机使用; 模块可被 macOS 安全导入 (CI 双平台跑单测):
所有 win32 专用调用都在函数体内按 sys.platform 门控, 习语同 volume_id.py。

绝不做裸扇区写入 —— 全部磁盘操作经 diskpart 脚本 + PowerShell 结构化查询。
diskpart 输出是控制台 OEM 码页 (中文 Windows = GBK); 退出码多数故障下仍是 0,
偶尔吐 HRESULT (2026-09-17 真机: 脚本命令失败回 0x80070057):
- 解码: 先试 UTF-8, 失败回退 mbcs/gbk (errors=replace)
- 解析锚点只用 locale 无关片段 (0xNN / 数字 / 盘符字母 / 中英行首词)
- 每步结果靠"语义后验"确认 (卷根存在 / 盘符消失), 错误关键词仅快速失败
- 报错文案取输出**尾部** —— diskpart 的报错行跟在成串的成功行后面
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.app.usb_vault import config


class DiskLayoutError(Exception):
    """分区布局操作失败 (权限 / 解析 / 语义后验不过 / 交叉验证不一致)。"""


# diskpart 卷表行首词按中英双写; 其余锚点 (0xNN / \d+ / 盘符字母) locale 无关
_VOLUME_ROW = re.compile(r"^\s*(?:卷|Volume)\s+\d+\s+([A-Z])\b", re.M)
# GPT 属性行的 16 位 hex 值 (diskpart 对 GPT 分区打印 "GPT 属性/Attributes:
# 0xNNNNNNNNNNNNNNNN", 标签随 locale, 值恒定) —— finalize 的语义后验用
_GPT_ATTRIBUTE_HEX = re.compile(r"0x([0-9A-Fa-f]{16})\b")
_DISKPART_ERR = re.compile(
    r"(?:遇到.{0,3}错误|encountered an error|拒绝访问|access is denied)", re.I
)


@dataclass(slots=True)
class DiskSummary:
    """clean 前给确认提示看的磁盘概要 (双通道交叉验证后的产物)。"""

    disk_number: int
    model: str  # Get-Disk FriendlyName
    size_bytes: int
    bus_type: str  # "USB" / 其他; 非 USB 拒绝 clean
    letters: list[str]  # 该盘全部已分配盘符


@dataclass(slots=True)
class PartitionInfo:
    number: int
    type_byte: int | None  # MBR: Get-Partition MbrType; GPT 磁盘恒 None
    gpt_type: str | None = None  # GPT: Get-Partition GptType GUID; MBR 恒 None
    has_letter: bool = False  # Get-Partition DriveLetter 非空


@dataclass(slots=True)
class DiskLayout:
    disk_number: int
    partition_style: str | None  # Get-Disk PartitionStyle: "MBR"/"GPT"; None=未知
    partitions: list[PartitionInfo]

    @property
    def _p2(self) -> PartitionInfo | None:
        found = [p for p in self.partitions if p.number == 2]
        return found[0] if found else None

    @property
    def dual_scheme(self) -> str | None:
        """既有双入口布局的制式 ('gpt'/'mbr'/None=不是本工具的双入口布局)。

        判据全走结构化字段 (不猜): GPT = 分区2 为微软基本数据分区且无盘符;
        MBR = 分区2 类型 0x17 (旧版, 待迁移) 或 0x07 且无盘符。有盘符的 0x07
        分区2 是"普通多分区 U 盘", 不认 —— 走清盘重建路径, 绝不误复用。

        注意此判据**不看内容**: v2.4.0 反转布局 (分区2=加密链) 与旧版布局
        (分区2=.app) 的表签名完全相同 —— 新旧判别在 burner.burn_dual 里做
        (临时挂载后看分区2有无 keyinfo), 旧盘一律走清盘重建。
        """
        p2 = self._p2
        if p2 is None:
            return None
        if self.partition_style == "GPT":
            if (
                p2.gpt_type or ""
            ).upper() == config.GPT_BASIC_DATA_TYPE.upper() and not p2.has_letter:
                return config.DUAL_SCHEME_GPT
            return None
        if self.partition_style == "MBR":
            if p2.type_byte == config.PART2_TYPE_HIDDEN_LEGACY:
                return config.DUAL_SCHEME_MBR  # 2026-09-16 旧版布局, 重烧时迁移
            if p2.type_byte == config.PART2_TYPE_EXFAT and not p2.has_letter:
                return config.DUAL_SCHEME_MBR
        return None

    @property
    def legacy_hidden_p2(self) -> bool:
        """分区2 是否还是旧版 0x17 (macOS 不挂载的元凶), refresh 时须先改回 0x07。"""
        p2 = self._p2
        return (
            p2 is not None
            and self.partition_style == "MBR"
            and p2.type_byte == config.PART2_TYPE_HIDDEN_LEGACY
        )

    @property
    def is_dual_entry(self) -> bool:
        """向后兼容的布尔形态 (是否本工具的双入口布局, 不分制式)。"""
        return self.dual_scheme is not None


# ---------------------------------------------------------------------------
# 权限与基础件
# ---------------------------------------------------------------------------


def is_admin() -> bool:
    """当前进程是否以管理员运行 (win32 专用; windll 访问在函数体内)。"""
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def require_admin() -> None:
    """双入口烧录的前置门: 非 Windows / 非管理员直接人话报错。"""
    if sys.platform != "win32":
        raise DiskLayoutError("双入口分区烧录仅在 Windows 烧录机执行")
    if not is_admin():
        raise DiskLayoutError("双入口分区烧录需要管理员权限: 请以管理员身份重新打开终端后重试")


def _decode_console(raw: bytes) -> str:
    """diskpart/PowerShell 控制台输出解码: 先 UTF-8, 失败回退 mbcs/gbk。

    中文 Windows 的 diskpart 输出是 OEM 码页 (cp936=GBK); 纯 UTF-8 解码会把
    CJK 锚点毁成 U+FFFD。mbcs 在非 Windows 平台不存在, 用 codecs 探测。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    import codecs

    for enc in ("mbcs", "gbk"):
        try:
            codecs.lookup(enc)
        except LookupError:
            continue
        return raw.decode(enc, errors="replace")
    return raw.decode("utf-8", errors="replace")


def run_diskpart(script: str, *, timeout_s: float = 180.0) -> str:
    """执行 diskpart 脚本, 返回 stdout 文本。

    脚本写入 ASCII 临时文件 (我们的命令全 ASCII, 规避码页问题)。
    失败判定双保险: 非 0 退出码 + 输出里的错误关键词 (退出码 0 也可能带错)。
    报错文案取尾部 400 字符 —— 2026-09-17 真机 0x80070057 的教训: 报错行
    在长串成功行之后, 截头部会把真正的失败原因切掉, 只看到已成功的步骤。
    """
    if sys.platform != "win32":
        raise DiskLayoutError("diskpart 仅在 Windows 可用")
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="usb_vault_dp_")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(script)
        try:
            proc = subprocess.run(["diskpart", "/s", path], capture_output=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise DiskLayoutError(f"diskpart 超时 ({timeout_s:.0f}s): 操作中止") from exc
        out = _decode_console(proc.stdout or b"")
        err = _decode_console(proc.stderr or b"")
        text = out + err
        if proc.returncode != 0:
            raise DiskLayoutError(f"diskpart 退出码 {proc.returncode}: …{text.strip()[-400:]}")
        if _DISKPART_ERR.search(text):
            raise DiskLayoutError(f"diskpart 报错: …{text.strip()[-400:]}")
        return out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _existing_letters() -> set[str]:
    """已占用盘符集合 (A..Z 中根路径存在者)。"""
    return {c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{c}:/").exists()}


def _free_drive_letter(exclude: frozenset[str] | set[str] = frozenset()) -> str:
    """第一个空闲盘符 (跳过 A/B 与已占用/exclude); 无空闲 => 拒绝。"""
    used = _existing_letters() | set(exclude)
    for c in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        if c not in used:
            return c
    raise DiskLayoutError("没有空闲盘符可临时挂载第2分区: 请移除多余设备后重试")


def _letter_of(drive: Path) -> str:
    """盘根路径 -> 单字母盘符 (大写); 非 X: 形态 => 拒绝。"""
    m = re.match(r"^([A-Za-z]):", str(drive))
    if not m:
        raise DiskLayoutError(f"需要 Windows 盘符路径 (如 E:): {drive}")
    return m.group(1).upper()


def viewer_part_size_mib(viewer_bytes: int) -> int:
    """分区1 (阅览器分区) 容量 (MiB): max(下限, 阅览器体积 × 放大系数 + 固定余量)。

    纯函数。v2.4.0 反转布局: 分区1只装双阅览器 ((WIN)/(MAC) 后缀名), 加密链
    进分区2 (剩余全部容量) —— 旧名 p2_size_mib (当年分区2装 .app)。
    2026-09-22 用户定稿: 下限抬到 1 GiB —— 分区1是唯一常驻盘符、拔插可见
    可读的入口, 余量直接给足, 不为省几百 MB 折损可靠性。
    """
    grown = math.ceil(viewer_bytes * config.VIEWER_PART_MARGIN_RATIO / (1024 * 1024))
    return max(config.VIEWER_PART_MIN_MIB, grown + config.VIEWER_PART_MARGIN_MIB)


# 重建脚本给分区1留的余量 (MiB): 吸收 MiB 取整与 1MB 对齐损耗。尾部这几 MiB
# 不进任何分区, 对 GB 级 U 盘可忽略; 换来的是 size 合计必然 < 可用容量。
_PROVISION_SLACK_MIB = 8


def _disk_size_mib(disk_number: int) -> int:
    """磁盘总容量 (MiB, 向下取整); 查不到/异常容量直接拒绝 —— 绝不在未知
    几何下执行 clean 这类破坏性重建。"""
    rows = _powershell_json(
        f"Get-Disk -Number {disk_number} -ErrorAction Stop | Select-Object Size | ConvertTo-Json"
    )
    if not rows:
        raise DiskLayoutError(f"磁盘 {disk_number}: Get-Disk 无返回, 拒绝重建")
    size_bytes = int(rows[0].get("Size") or 0)
    size_mib = size_bytes // (1024 * 1024)
    if size_mib <= 0:
        raise DiskLayoutError(f"磁盘 {disk_number}: 容量异常 ({size_bytes} 字节), 拒绝重建")
    return size_mib


def _root_exists(letter: str) -> bool:
    """语义后验接缝: 临时盘符是否已挂载出卷根。"""
    return Path(f"{letter}:/").exists()


# ---------------------------------------------------------------------------
# PowerShell 结构化查询 + 交叉验证
# ---------------------------------------------------------------------------


def _powershell_json(command: str, *, timeout_s: float = 30.0) -> list[Any]:
    """执行 PowerShell 查询并按 JSON 解析; 单对象归一为列表。

    强制 [Console]::OutputEncoding=UTF8, 规避 5.1 默认 OEM 管道编码问题。
    """
    if sys.platform != "win32":
        raise DiskLayoutError("PowerShell 查询仅在 Windows 可用")
    prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + command],
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise DiskLayoutError(f"PowerShell 查询超时 ({timeout_s:.0f}s)") from exc
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise DiskLayoutError(f"PowerShell 查询失败: {err[:200] or f'退出码 {proc.returncode}'}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DiskLayoutError(f"PowerShell 输出无法解析: {out.strip()[:200]!r}") from exc
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if not isinstance(data, list):
        raise DiskLayoutError(f"PowerShell 输出不是对象列表: {out.strip()[:200]!r}")
    return data


def resolve_disk(letter: str) -> DiskSummary:
    """盘符 -> 磁盘概要, 双通道交叉验证 (clean 前的保命步骤)。

    通道1 PowerShell 结构化查询 (无 locale 问题); 通道2 diskpart detail disk
    的卷表 —— 两通道必须指向同一块盘, 且 BusType 必须是 USB (绝不 clean
    内置盘), 否则拒绝。
    """
    rows = _powershell_json(
        f"Get-Partition -DriveLetter {letter} -ErrorAction Stop "
        "| Select-Object DiskNumber,PartitionNumber,DriveLetter | ConvertTo-Json"
    )
    disk_numbers = {int(r["DiskNumber"]) for r in rows if r.get("DiskNumber") is not None}
    if len(disk_numbers) != 1:
        raise DiskLayoutError(f"盘符 {letter}: 无法唯一确定磁盘 (Get-Partition 返回 {rows!r})")
    disk_number = disk_numbers.pop()

    drows = _powershell_json(
        f"Get-Disk -Number {disk_number} -ErrorAction Stop "
        "| Select-Object FriendlyName,Size,BusType | ConvertTo-Json"
    )
    if not drows:
        raise DiskLayoutError(f"磁盘 {disk_number}: Get-Disk 无返回")
    d = drows[0]
    model = str(d.get("FriendlyName") or "未知型号")
    size_bytes = int(d.get("Size") or 0)
    bus_type = str(d.get("BusType") or "")

    prows = _powershell_json(
        f"Get-Partition -DiskNumber {disk_number} -ErrorAction Stop "
        "| Select-Object DriveLetter | ConvertTo-Json"
    )
    letters = sorted({str(r["DriveLetter"]).upper() for r in prows if r.get("DriveLetter")})

    detail = run_diskpart(f"select disk {disk_number}\ndetail disk\n")
    dp_letters = set(_VOLUME_ROW.findall(detail))
    if not dp_letters or letter not in dp_letters:
        raise DiskLayoutError(
            f"盘符 {letter}: PowerShell 指向磁盘 {disk_number}, 但 diskpart 卷表 "
            f"{sorted(dp_letters)} 不含该盘符 —— 交叉验证不过, 拒绝操作"
        )

    if bus_type.upper() != "USB":
        raise DiskLayoutError(
            f"磁盘 {disk_number} ({model}) 不是 USB 总线 ({bus_type or '未知'}): 拒绝重新分区"
        )
    return DiskSummary(
        disk_number=disk_number,
        model=model,
        size_bytes=size_bytes,
        bus_type=bus_type.upper(),
        letters=letters,
    )


# ---------------------------------------------------------------------------
# 布局探测与 provision / refresh / finalize
# ---------------------------------------------------------------------------


def inspect_layout(disk_number: int) -> DiskLayout:
    """读回磁盘分区概要: 分区表制式 + 各分区 (号/MBR 类型字节/GPT GUID/盘符)。

    全走 Get-Disk/Get-Partition 结构化通道 —— GPT 分区没有 0xNN 类型字节,
    diskpart 明文输出又依赖 locale; PowerShell JSON 两者都免疫 (2026-09-17
    制式重构: 0x17 弃用后 MBR/GPT 都要识别)。字段解析不到 => None =>
    宁可不识别为双入口布局, 也不猜。
    """
    drows = _powershell_json(
        f"Get-Disk -Number {disk_number} -ErrorAction Stop "
        "| Select-Object PartitionStyle | ConvertTo-Json"
    )
    style = str(drows[0].get("PartitionStyle") or "").strip() if drows else ""
    prows = _powershell_json(
        f"Get-Partition -DiskNumber {disk_number} -ErrorAction Stop "
        "| Select-Object PartitionNumber,DriveLetter,MbrType,GptType | ConvertTo-Json"
    )
    if not prows:
        raise DiskLayoutError(f"磁盘 {disk_number}: 查询不到任何分区 —— 拒绝继续 (宁可报错不猜)")
    partitions = [
        PartitionInfo(
            number=int(r["PartitionNumber"]),
            type_byte=int(r["MbrType"]) if r.get("MbrType") is not None else None,
            gpt_type=str(r["GptType"]) if r.get("GptType") else None,
            has_letter=bool(r.get("DriveLetter")),
        )
        for r in prows
    ]
    return DiskLayout(disk_number=disk_number, partition_style=style or None, partitions=partitions)


# 慢棒卷枚举竞态的等待参数: 30 次 × 0.5s = 最长 15s (白牌棒实测枚举延迟
# 秒级; 超时报错并让操作者重跑自愈, 绝不带着未枚举的卷硬走)
_VOLUME_WAIT_ATTEMPTS = 30
_VOLUME_WAIT_INTERVAL_S = 0.5


def _partition_volume_letter(disk_number: int, part_no: int) -> str | None:
    """分区当前卷的盘符: ''=卷已枚举但无盘符 / None=卷尚未枚举 (查询空输出)。

    Get-Partition | Get-Volume 在卷未枚举时输出为空 => _powershell_json
    抛 JSON 解析错 —— 这里吞掉当"未就绪", 由调用方轮询。
    """
    try:
        rows = _powershell_json(
            f"Get-Partition -DiskNumber {disk_number} -PartitionNumber {part_no} "
            "-ErrorAction Stop | Get-Volume -ErrorAction SilentlyContinue "
            "| Select-Object DriveLetter | ConvertTo-Json"
        )
    except DiskLayoutError:
        return None
    if not rows:
        return None
    return str(rows[0].get("DriveLetter") or "").upper()


def _ensure_partition_letter(disk_number: int, part_no: int, letter: str) -> None:
    """等分区的卷枚举出来, 并落指定盘符。

    2026-09-22 真机教训 (同日第二轮): 慢棒上 create partition 返回时卷对象
    尚未枚举, assign 与 format 一样要卷焦点 —— 同一脚本里 create 后紧跟哪条
    都报 "There is no volume specified" (0x80070057)。上轮"assign 先于
    format"只挪了报错点, 没治竞态; 治法是轮询等卷出现再操作。

    挂载管理器可能抢先自动补盘符 (clean 后原盘符空出, 可移动盘的 RAW 卷
    常被自动挂载): 恰是目标盘符 => 免 assign; 抢成别的 => 先摘再指 (卷挂
    多个盘符会在资源管理器重复显示); 无盘符 => 直接 assign。
    """
    for _ in range(_VOLUME_WAIT_ATTEMPTS):
        current = _partition_volume_letter(disk_number, part_no)
        if current is not None:
            if current == letter:
                return
            extra = f"remove letter={current}\n" if current else ""
            run_diskpart(
                f"select disk {disk_number}\nselect partition {part_no}\n"
                f"{extra}assign letter={letter}\n"
            )
            return
        time.sleep(_VOLUME_WAIT_INTERVAL_S)
    raise DiskLayoutError(
        f"磁盘 {disk_number} 分区 {part_no}: 等待 "
        f"{_VOLUME_WAIT_ATTEMPTS * _VOLUME_WAIT_INTERVAL_S:.0f}s 卷仍未枚举 "
        "(慢棒竞态): 直接重跑同一烧录命令即可恢复 (clean 起步)"
    )


def provision_dual_layout(
    disk_number: int, letter: str, p1_mib: int, *, scheme: str = config.DUAL_SCHEME_GPT
) -> Path:
    """全盘重建为双入口布局 (调用前必须已过人工确认!)。

    v2.4.0 反转布局: 分区1 = p1_mib MiB (双阅览器, (WIN)/(MAC) 后缀名),
    保留原盘符; 分区2 = 总容量 − p1 − 余量 (**加密链全套**, 先临时挂盘符,
    finalize 时再隐藏)。scheme 决定分区表: gpt (默认, 交付级) / mbr
    (快速验证制式)。返回第2分区的临时盘符根。
    2026-09-28 起两分区一律 format fs=ntfs (用户评审通过的 NTFS 交付形态):
    防误删靠 NTFS deny ACE (ntfs_guard), exFAT 无 ACL 无从施加; 序列号
    锚点对 NTFS 同样支持 (boot_sector @0x48)。

    2026-09-17 真机教训: 分区1的 create 不能省 size= —— diskpart 的裸
    `create partition primary` 会吃满全盘剩余空间, 随后的分区2 create 在
    零可用空间上失败 (diskpart 退出码 0x80070057, 盘停在半重建状态)。
    所以两个分区都必须显式限宽, 合计必然小于实测量化后的可用容量
    (分区2 = 总量 − p1 − slack, 同一约束的另一半)。

    2026-09-18 真机教训: clean 后的盘是未初始化 RAW, Win11 24H2
    diskpart 10.0.26100 对它直接 `convert gpt` 报 "not MBR formatted"
    (退出码 0x80070057) —— convert gpt 的前提是"空 **MBR** 盘"。gpt 制式
    须两连转: 先 convert mbr 把裸盘初始化成 MBR, 再 convert gpt 翻成
    GPT; 两步都是纯元数据操作, 瞬时且无额外破坏。

    2026-09-22 真机教训 (同日两轮): 白牌慢速棒 (Generic STORAGE DEVICE
    115GB) 上 `create partition` 返回时卷对象尚未枚举 —— format 和 assign
    都需要卷焦点, 同一脚本里紧跟哪条都会报 "There is no volume
    specified" (0x80070057)。静态 diskpart 脚本无法等待, 故拆三段:
    ①两个分区一次建完 (纯分区表操作, 无卷焦点需求); ②每分区在 Python
    侧轮询 Get-Volume 等卷枚举出来并落盘符 (_ensure_partition_letter,
    顺带处理挂载管理器抢先自动补的盘符); ③format 落在已枚举的卷上。
    附带自愈性: 任一段失败, 重跑同一烧录命令 (clean 开头) 即恢复, 不会
    停在"无盘符"的死状态。
    """
    if scheme not in config.DUAL_SCHEMES:
        raise DiskLayoutError(f"未知双入口制式: {scheme!r} (可选 {config.DUAL_SCHEMES})")
    temp = _free_drive_letter(exclude={letter})
    total_mib = _disk_size_mib(disk_number)
    p2_mib = total_mib - p1_mib - _PROVISION_SLACK_MIB
    if p2_mib <= 0:
        raise DiskLayoutError(
            f"磁盘 {disk_number} 总容量 {total_mib} MiB 装不下双分区 "
            f"(分区1就要 {p1_mib} MiB): 拒绝重建"
        )
    convert = "convert mbr\nconvert gpt" if scheme == config.DUAL_SCHEME_GPT else "convert mbr"
    script_create = (
        f"select disk {disk_number}\n"
        "clean\n"
        f"{convert}\n"
        f"create partition primary size={p1_mib}\n"
        f"create partition primary size={p2_mib}\n"
    )
    run_diskpart(script_create)
    _ensure_partition_letter(disk_number, 1, letter)
    run_diskpart(f"select disk {disk_number}\nselect partition 1\nformat fs=ntfs quick\n")
    _ensure_partition_letter(disk_number, 2, temp)
    run_diskpart(f"select disk {disk_number}\nselect partition 2\nformat fs=ntfs quick\n")
    if not _root_exists(letter):
        raise DiskLayoutError(f"重建后分区1未挂载到原盘符 {letter}:")
    if not _root_exists(temp):
        raise DiskLayoutError(f"重建后分区2未挂载到临时盘符 {temp}:")
    # 防盘符漂移: clean 后极小概率有别的设备抢占了原盘符 —— 复核指向仍是本盘
    again = resolve_disk(letter)
    if again.disk_number != disk_number:
        raise DiskLayoutError(
            f"盘符 {letter} 重建后指向了磁盘 {again.disk_number} (期望 {disk_number}): 拒绝写入"
        )
    return Path(f"{temp}:/")


def refresh_partition2(disk_number: int, *, migrate_legacy_mbr: bool = False) -> Path:
    """复用既有双入口布局: 给分区2临时挂盘符 (或复用上次 finalize 失败残留的), 返回其根。

    migrate_legacy_mbr=True 时先把 0x17 改回 0x07 (2026-09-16 旧版盘迁移):
    **必须发生在 assign letter 之前** —— Windows 对隐藏类型分区不挂载文件
    系统, assign 后卷根不出现, refresh 会误报失败。
    2026-09-22 真机同构追加: 已交付盘分区2带 GPT 无盘符属性, 同样必须
    先清再 assign —— 直接 assign 挂不出卷根, 复烧报"分区2未能挂载到
    临时盘符"中止。只对 GPT 盘发这条命令 (MBR 盘上 gpt attributes 报错);
    收尾 finalize 会把属性原样落回, 分区2最终形态不变。
    """
    if migrate_legacy_mbr:
        run_diskpart(
            f"select disk {disk_number}\nselect partition 2\n"
            f"set id={config.PART2_TYPE_EXFAT:#04x} override\n"
        )
    rows = _powershell_json(
        f"Get-Partition -DiskNumber {disk_number} -ErrorAction Stop "
        "| Where-Object PartitionNumber -eq 2 "
        "| Select-Object DriveLetter | ConvertTo-Json"
    )
    existing = [str(r["DriveLetter"]).upper() for r in rows if r.get("DriveLetter")]
    if existing:
        return Path(f"{existing[0]}:/")
    # 分区表制式: 决定清隐藏属性用哪条命令 (gpt attributes 对 MBR 盘是错误命令)
    style_rows = _powershell_json(
        f"Get-Disk -Number {disk_number} -ErrorAction Stop "
        "| Select-Object PartitionStyle | ConvertTo-Json"
    )
    is_gpt = bool(style_rows) and str(style_rows[0].get("PartitionStyle", "")).upper() == "GPT"
    clear = f"gpt attributes={0:#018x}\n" if is_gpt else ""
    temp = _free_drive_letter()
    run_diskpart(f"select disk {disk_number}\nselect partition 2\n{clear}assign letter={temp}\n")
    if not _root_exists(temp):
        raise DiskLayoutError(f"分区2未能挂载到临时盘符 {temp}:")
    return Path(f"{temp}:/")


def finalize_partition2(
    disk_number: int, temp_letter: str, *, scheme: str = config.DUAL_SCHEME_GPT
) -> None:
    """烧录收尾: 摘掉分区2盘符 + 按制式落隐藏位 (2026-09-22 用户再次确认:
    分区2保持无盘符隐藏, 分区1才是拔插可见可读的入口)。

    gpt: 设无盘符属性 0x8000000000000000 (写盘上, 换机生效) —— 落完读回
    detail partition 语义后验属性位, 坏盘不出仓; mbr: 类型字节钉在 0x07
    (exFAT 标准型, 新建布局本就是 0x07, 此处幂等兜住旧 0x17 盘)。
    """
    if scheme not in config.DUAL_SCHEMES:
        raise DiskLayoutError(f"未知双入口制式: {scheme!r} (可选 {config.DUAL_SCHEMES})")
    hide = (
        f"gpt attributes={config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER:#018x}\n"
        if scheme == config.DUAL_SCHEME_GPT
        else f"set id={config.PART2_TYPE_EXFAT:#04x} override\n"
    )
    run_diskpart(
        f"select disk {disk_number}\nselect partition 2\nremove letter={temp_letter}\n{hide}"
    )
    if _root_exists(temp_letter):
        raise DiskLayoutError(f"分区2盘符 {temp_letter} 未能摘除: 请在磁盘管理手动处理")
    if scheme == config.DUAL_SCHEME_GPT:
        # 语义后验: 属性位必须真的落上 (盘符摘了但属性没落 = 换台 Windows 就现身)
        if not _p2_attribute_in_place(disk_number):
            raise DiskLayoutError(
                "分区2 GPT 无盘符属性未生效 (detail partition 未读到属性位): "
                "请在磁盘管理核对分区2属性后重跑"
            )


def _p2_attribute_in_place(disk_number: int) -> bool:
    """语义后验读端: detail partition 读回分区2属性位, 无盘符位在位才算数。"""
    detail = run_diskpart(f"select disk {disk_number}\nselect partition 2\ndetail partition\n")
    hexes = _GPT_ATTRIBUTE_HEX.findall(detail)
    return bool(hexes) and bool(int(hexes[0], 16) & config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER)


# ---------------------------------------------------------------------------
# 跨平台自愈 (2026-09-29 实机定案): macOS 不认微软专属的 GPT 无盘符属性
# (挂载照常, 这正是查看器 /Volumes 通道的立足点), 且改写分区表时会把该
# 属性位剥掉 —— 收件 Windows 下次插入就给分区2补上盘符。属性位只能在
# 烧录机 (管理员) 侧恢复: verify 读态报警, heal 免重烧修复。
# ---------------------------------------------------------------------------


def disk_number_of(letter: str) -> int:
    """盘符 -> 磁盘号 (轻量只读通道): 只查 Get-Partition, 不做 diskpart
    交叉验证与 USB 闸 —— 供 verify 的只读检查用; 动盘操作仍必须走
    resolve_disk 的强通道。"""
    rows = _powershell_json(
        f"Get-Partition -DriveLetter {letter} -ErrorAction Stop "
        "| Select-Object DiskNumber | ConvertTo-Json"
    )
    nums = {int(r["DiskNumber"]) for r in rows if r.get("DiskNumber") is not None}
    if len(nums) != 1:
        raise DiskLayoutError(f"盘符 {letter}: 无法唯一确定磁盘 (Get-Partition 返回 {rows!r})")
    return nums.pop()


def partition2_state(disk_number: int) -> dict[str, Any] | None:
    """只读探测分区2隐藏态 (verify / heal 的共同读端)。

    返回 {'letters': [...], 'is_gpt': bool, 'attr_ok': bool}; 磁盘没有
    分区2 (单分区形态) 返回 None。attr_ok 仅对 GPT 有意义 (MBR 制式的
    隐藏靠类型字节, 早已换代退役)。GptAttributes 在 PowerShell 侧先格式化
    成 16 位十六进制串再进 JSON —— uint64 直接序列化有 int64 溢出风险。
    Get-Partition 普通权限可跑, verify 因此不需要管理员。"""
    rows = _powershell_json(
        f"Get-Partition -DiskNumber {disk_number} -ErrorAction Stop "
        "| Select-Object PartitionNumber,DriveLetter,GptType,"
        "@{n='GptAttributes';e={'0x{0:x16}' -f $_.GptAttributes}} | ConvertTo-Json"
    )
    for r in rows:
        try:
            if int(r.get("PartitionNumber") or 0) != 2:
                continue
        except (TypeError, ValueError):
            continue
        letters = [str(r["DriveLetter"]).upper()] if r.get("DriveLetter") else []
        gpt_type = str(r.get("GptType") or "").strip()
        m = _GPT_ATTRIBUTE_HEX.search(str(r.get("GptAttributes") or ""))
        attr_ok = (m is not None
                   and bool(int(m.group(1), 16) & config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER))
        return {"letters": letters, "is_gpt": bool(gpt_type), "attr_ok": attr_ok}
    return None


def heal_partition2_hidden(disk_number: int) -> list[str]:
    """恢复分区2隐藏形态 (写端, 烧录机管理员): macOS 用过的双分区盘,
    属性位被剥 (有时还挂着 Windows 补的盘符) —— 摘盘符 + 重落属性位 +
    detail partition 读回后验, 与 finalize_partition2 同构, 坏盘不出仓。
    返回本次摘掉的盘符列表 (空表 = 只补了属性位)。"""
    state = partition2_state(disk_number)
    if state is None:
        raise DiskLayoutError("磁盘没有分区2: 无隐藏形态可恢复 (单分区形态?)")
    if not state["is_gpt"]:
        raise DiskLayoutError("分区2不是 GPT 制式 (旧 MBR 盘无属性位): 请重烧换代")
    letters: list[str] = state["letters"]
    removes = "".join(f"remove letter={L}\n" for L in letters)
    run_diskpart(
        f"select disk {disk_number}\nselect partition 2\n{removes}"
        f"gpt attributes={config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER:#018x}\n"
    )
    for letter in letters:
        if _root_exists(letter):
            raise DiskLayoutError(f"分区2盘符 {letter} 未能摘除: 请在磁盘管理手动处理")
    if not _p2_attribute_in_place(disk_number):
        raise DiskLayoutError(
            "分区2 GPT 无盘符属性未生效 (detail partition 未读到属性位): "
            "请在磁盘管理核对分区2属性后重跑"
        )
    return letters
