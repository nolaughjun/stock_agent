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
    r"(?:遇到.{0,3}错误|encountered an error|拒绝访问|access is denied)", re.I)


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
        """
        p2 = self._p2
        if p2 is None:
            return None
        if self.partition_style == "GPT":
            if (p2.gpt_type or "").upper() == config.GPT_BASIC_DATA_TYPE.upper() \
                    and not p2.has_letter:
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
        return (p2 is not None and self.partition_style == "MBR"
                and p2.type_byte == config.PART2_TYPE_HIDDEN_LEGACY)

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
            proc = subprocess.run(["diskpart", "/s", path], capture_output=True,
                                  timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise DiskLayoutError(f"diskpart 超时 ({timeout_s:.0f}s): 操作中止") from exc
        out = _decode_console(proc.stdout or b"")
        err = _decode_console(proc.stderr or b"")
        text = out + err
        if proc.returncode != 0:
            raise DiskLayoutError(
                f"diskpart 退出码 {proc.returncode}: …{text.strip()[-400:]}")
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


def p2_size_mib(viewer_bytes: int) -> int:
    """第2分区容量 (MiB): max(下限, .app 体积 × 放大系数 + 固定余量)。纯函数。"""
    grown = math.ceil(viewer_bytes * config.PART2_SIZE_MARGIN_RATIO / (1024 * 1024))
    return max(config.PART2_SIZE_MIN_MIB, grown + config.PART2_SIZE_MARGIN_MIB)


# 重建脚本给分区1留的余量 (MiB): 吸收 MiB 取整与 1MB 对齐损耗。尾部这几 MiB
# 不进任何分区, 对 GB 级 U 盘可忽略; 换来的是 size 合计必然 < 可用容量。
_PROVISION_SLACK_MIB = 8


def _disk_size_mib(disk_number: int) -> int:
    """磁盘总容量 (MiB, 向下取整); 查不到/异常容量直接拒绝 —— 绝不在未知
    几何下执行 clean 这类破坏性重建。"""
    rows = _powershell_json(
        f"Get-Disk -Number {disk_number} -ErrorAction Stop "
        "| Select-Object Size | ConvertTo-Json")
    if not rows:
        raise DiskLayoutError(f"磁盘 {disk_number}: Get-Disk 无返回, 拒绝重建")
    size_bytes = int(rows[0].get("Size") or 0)
    size_mib = size_bytes // (1024 * 1024)
    if size_mib <= 0:
        raise DiskLayoutError(
            f"磁盘 {disk_number}: 容量异常 ({size_bytes} 字节), 拒绝重建")
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
            capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise DiskLayoutError(f"PowerShell 查询超时 ({timeout_s:.0f}s)") from exc
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise DiskLayoutError(
            f"PowerShell 查询失败: {err[:200] or f'退出码 {proc.returncode}'}")
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
        "| Select-Object DiskNumber,PartitionNumber,DriveLetter | ConvertTo-Json")
    disk_numbers = {int(r["DiskNumber"]) for r in rows if r.get("DiskNumber") is not None}
    if len(disk_numbers) != 1:
        raise DiskLayoutError(f"盘符 {letter}: 无法唯一确定磁盘 (Get-Partition 返回 {rows!r})")
    disk_number = disk_numbers.pop()

    drows = _powershell_json(
        f"Get-Disk -Number {disk_number} -ErrorAction Stop "
        "| Select-Object FriendlyName,Size,BusType | ConvertTo-Json")
    if not drows:
        raise DiskLayoutError(f"磁盘 {disk_number}: Get-Disk 无返回")
    d = drows[0]
    model = str(d.get("FriendlyName") or "未知型号")
    size_bytes = int(d.get("Size") or 0)
    bus_type = str(d.get("BusType") or "")

    prows = _powershell_json(
        f"Get-Partition -DiskNumber {disk_number} -ErrorAction Stop "
        "| Select-Object DriveLetter | ConvertTo-Json")
    letters = sorted({str(r["DriveLetter"]).upper() for r in prows if r.get("DriveLetter")})

    detail = run_diskpart(f"select disk {disk_number}\ndetail disk\n")
    dp_letters = set(_VOLUME_ROW.findall(detail))
    if not dp_letters or letter not in dp_letters:
        raise DiskLayoutError(
            f"盘符 {letter}: PowerShell 指向磁盘 {disk_number}, 但 diskpart 卷表 "
            f"{sorted(dp_letters)} 不含该盘符 —— 交叉验证不过, 拒绝操作")

    if bus_type.upper() != "USB":
        raise DiskLayoutError(
            f"磁盘 {disk_number} ({model}) 不是 USB 总线 ({bus_type or '未知'}): 拒绝重新分区")
    return DiskSummary(disk_number=disk_number, model=model, size_bytes=size_bytes,
                       bus_type=bus_type.upper(), letters=letters)


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
        "| Select-Object PartitionStyle | ConvertTo-Json")
    style = str(drows[0].get("PartitionStyle") or "").strip() if drows else ""
    prows = _powershell_json(
        f"Get-Partition -DiskNumber {disk_number} -ErrorAction Stop "
        "| Select-Object PartitionNumber,DriveLetter,MbrType,GptType | ConvertTo-Json")
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
    return DiskLayout(disk_number=disk_number,
                      partition_style=style or None, partitions=partitions)


def provision_dual_layout(disk_number: int, letter: str, p2_mib: int, *,
                          scheme: str = config.DUAL_SCHEME_GPT) -> Path:
    """全盘重建为双入口布局 (调用前必须已过人工确认!)。

    分区1 = 总容量 − 分区2 − 余量 (数据 + Windows 查看器), 保留原盘符;
    分区2 = p2_mib MiB (仅 Mac 查看器), 先临时挂盘符, finalize 时再隐藏。
    scheme 决定分区表: gpt (默认, 交付级) / mbr (快速验证制式)。返回第2
    分区的临时盘符根。

    2026-09-17 真机教训: 分区1的 create 不能省 size= —— diskpart 的裸
    `create partition primary` 会吃满全盘剩余空间, 随后的分区2 create 在
    零可用空间上失败 (diskpart 退出码 0x80070057, 盘停在半重建状态)。
    所以两个分区都必须显式限宽, 合计必然小于实测量化后的可用容量。

    2026-09-18 真机教训: clean 后的盘是未初始化 RAW, Win11 24H2
    diskpart 10.0.26100 对它直接 `convert gpt` 报 "not MBR formatted"
    (退出码 0x80070057) —— convert gpt 的前提是"空 **MBR** 盘"。gpt 制式
    须两连转: 先 convert mbr 把裸盘初始化成 MBR, 再 convert gpt 翻成
    GPT; 两步都是纯元数据操作, 瞬时且无额外破坏。
    """
    if scheme not in config.DUAL_SCHEMES:
        raise DiskLayoutError(f"未知双入口制式: {scheme!r} (可选 {config.DUAL_SCHEMES})")
    temp = _free_drive_letter(exclude={letter})
    total_mib = _disk_size_mib(disk_number)
    p1_mib = total_mib - p2_mib - _PROVISION_SLACK_MIB
    if p1_mib <= 0:
        raise DiskLayoutError(
            f"磁盘 {disk_number} 总容量 {total_mib} MiB 装不下双分区 "
            f"(分区2就要 {p2_mib} MiB): 拒绝重建")
    convert = ("convert mbr\nconvert gpt" if scheme == config.DUAL_SCHEME_GPT
               else "convert mbr")
    script = (
        f"select disk {disk_number}\n"
        "clean\n"
        f"{convert}\n"
        f"create partition primary size={p1_mib}\n"
        "format fs=exfat quick\n"
        f"assign letter={letter}\n"
        f"create partition primary size={p2_mib}\n"
        "format fs=exfat quick\n"
        f"assign letter={temp}\n"
    )
    run_diskpart(script)
    if not _root_exists(letter):
        raise DiskLayoutError(f"重建后分区1未挂载到原盘符 {letter}:")
    if not _root_exists(temp):
        raise DiskLayoutError(f"重建后分区2未挂载到临时盘符 {temp}:")
    # 防盘符漂移: clean 后极小概率有别的设备抢占了原盘符 —— 复核指向仍是本盘
    again = resolve_disk(letter)
    if again.disk_number != disk_number:
        raise DiskLayoutError(
            f"盘符 {letter} 重建后指向了磁盘 {again.disk_number} (期望 {disk_number}): 拒绝写入")
    return Path(f"{temp}:/")


def refresh_partition2(disk_number: int, *, migrate_legacy_mbr: bool = False) -> Path:
    """复用既有双入口布局: 给分区2临时挂盘符 (或复用上次 finalize 失败残留的), 返回其根。

    migrate_legacy_mbr=True 时先把 0x17 改回 0x07 (2026-09-16 旧版盘迁移):
    **必须发生在 assign letter 之前** —— Windows 对隐藏类型分区不挂载文件
    系统, assign 后卷根不出现, refresh 会误报失败。
    """
    if migrate_legacy_mbr:
        run_diskpart(
            f"select disk {disk_number}\nselect partition 2\n"
            f"set id={config.PART2_TYPE_EXFAT:#04x} override\n")
    rows = _powershell_json(
        f"Get-Partition -DiskNumber {disk_number} -ErrorAction Stop "
        "| Where-Object PartitionNumber -eq 2 "
        "| Select-Object DriveLetter | ConvertTo-Json")
    existing = [str(r["DriveLetter"]).upper() for r in rows if r.get("DriveLetter")]
    if existing:
        return Path(f"{existing[0]}:/")
    temp = _free_drive_letter()
    run_diskpart(
        f"select disk {disk_number}\nselect partition 2\nassign letter={temp}\n")
    if not _root_exists(temp):
        raise DiskLayoutError(f"分区2未能挂载到临时盘符 {temp}:")
    return Path(f"{temp}:/")


def finalize_partition2(disk_number: int, temp_letter: str, *,
                        scheme: str = config.DUAL_SCHEME_GPT) -> None:
    """烧录收尾: 摘掉分区2盘符 + 按制式落隐藏位。

    gpt: 设无盘符属性 0x8000000000000000 (写盘上, 换机生效) —— 落完读回
    detail partition 语义后验属性位, 坏盘不出仓; mbr: 类型字节钉在 0x07
    (exFAT 标准型, 新建布局本就是 0x07, 此处幂等兜住旧 0x17 盘)。
    """
    if scheme not in config.DUAL_SCHEMES:
        raise DiskLayoutError(f"未知双入口制式: {scheme!r} (可选 {config.DUAL_SCHEMES})")
    hide = (f"gpt attributes={config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER:#018x}\n"
            if scheme == config.DUAL_SCHEME_GPT
            else f"set id={config.PART2_TYPE_EXFAT:#04x} override\n")
    run_diskpart(
        f"select disk {disk_number}\nselect partition 2\n"
        f"remove letter={temp_letter}\n"
        f"{hide}")
    if _root_exists(temp_letter):
        raise DiskLayoutError(f"分区2盘符 {temp_letter} 未能摘除: 请在磁盘管理手动处理")
    if scheme == config.DUAL_SCHEME_GPT:
        # 语义后验: 属性位必须真的落上 (盘符摘了但属性没落 = 换台 Windows 就现身)
        detail = run_diskpart(
            f"select disk {disk_number}\nselect partition 2\ndetail partition\n")
        hexes = _GPT_ATTRIBUTE_HEX.findall(detail)
        if not hexes or not (int(hexes[0], 16) & config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER):
            raise DiskLayoutError(
                "分区2 GPT 无盘符属性未生效 (detail partition 未读到属性位): "
                "请在磁盘管理核对分区2属性后重跑")
