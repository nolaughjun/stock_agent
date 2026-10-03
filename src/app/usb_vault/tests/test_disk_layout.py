"""disk_layout 单测: fake subprocess (diskpart/PowerShell), 零真硬件依赖。

平台门控经 sys.platform 注入绕开 (本 CI 是 Windows 也照测 darwin 分支),
手法同 test_mac_volume; run_diskpart 的 fake 读回临时脚本文件内容供断言。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from src.app.usb_vault import config, disk_layout
from src.app.usb_vault.disk_layout import DiskLayoutError, DiskSummary


class _FakeCompleted:
    def __init__(self, rc: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")


def _patch_diskpart(
    monkeypatch: pytest.MonkeyPatch, outputs: _FakeCompleted | list[_FakeCompleted]
) -> tuple[list[str], list[str]]:
    """替身 diskpart: 记录脚本内容与临时文件路径, 依次回放 outputs。"""
    scripts: list[str] = []
    paths: list[str] = []
    queue = list(outputs) if isinstance(outputs, list) else [outputs]

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        paths.append(args[2])
        scripts.append(Path(args[2]).read_text(encoding="ascii"))
        return queue.pop(0) if queue else _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return scripts, paths


def _ps(obj: Any) -> bytes:
    """PowerShell 侧 fake 回包 (OutputEncoding 已被强制为 UTF-8)。"""
    return json.dumps(obj).encode("utf-8")


def _patch_volume_letters(
    monkeypatch: pytest.MonkeyPatch, p1: str | None, p2: str | None
) -> None:
    """伪造 provision 的卷盘符轮询 (_partition_volume_letter)。

    'X'=卷已枚举且挂了盘符 X / ''=卷已枚举无盘符 / None=卷尚未枚举 (竞态中)。
    """
    monkeypatch.setattr(
        disk_layout, "_partition_volume_letter", lambda disk, part: {1: p1, 2: p2}[part]
    )


# ---------------------------------------------------------------------------
# M1: 平台门控 / 管理员 / run_diskpart / 盘符 / 尺寸
# ---------------------------------------------------------------------------


def test_platform_gate_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(DiskLayoutError, match="仅.*Windows"):
        disk_layout.require_admin()
    with pytest.raises(DiskLayoutError, match="仅.*Windows"):
        disk_layout.run_diskpart("list disk\n")
    assert disk_layout.is_admin() is False


def test_require_admin_denied_message(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    monkeypatch.setattr(disk_layout, "is_admin", lambda: False)
    with pytest.raises(DiskLayoutError, match="管理员"):
        disk_layout.require_admin()


def test_run_diskpart_script_roundtrip_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    scripts, paths = _patch_diskpart(monkeypatch, _FakeCompleted(0, stdout=b"  Disk 3\r\n"))
    out = disk_layout.run_diskpart("select disk 3\nlist disk\n")
    assert "Disk 3" in out
    assert scripts == ["select disk 3\nlist disk\n"]
    assert not Path(paths[0]).exists()  # 临时脚本文件已删


def test_run_diskpart_rc_nonzero_raises(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    _patch_diskpart(monkeypatch, _FakeCompleted(1, stderr=b"boom"))
    with pytest.raises(DiskLayoutError, match="退出码 1"):
        disk_layout.run_diskpart("clean\n")


def test_run_diskpart_error_shows_tail_not_head(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """2026-09-17 真机: 0x80070057 的报错行在长串成功行之后, 旧版只截输出
    前 300 字符 —— 用户看到的全是"成功", 真正的失败原因被切掉了。"""
    noise = "DiskPart succeeded in cleaning the disk.\r\n" * 20  # >400 字符
    out = noise + "create partition primary size=257\r\nDiskPart 遇到一个错误: 参数错误。\r\n"
    _patch_diskpart(monkeypatch, _FakeCompleted(0x80070057, stdout=out.encode("gbk")))
    with pytest.raises(DiskLayoutError) as ei:
        disk_layout.run_diskpart("clean\n")
    msg = str(ei.value)
    assert "2147942487" in msg  # 0x80070057 的十进制, 与真机一致
    assert "参数错误" in msg  # 尾部的报错行必须保留


def test_run_diskpart_error_marker_gbk(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    """中文 Windows diskpart: 错误句以 GBK 回传且退出码仍是 0 —— 关键词必须命中。"""
    _patch_diskpart(
        monkeypatch, _FakeCompleted(0, stdout="DiskPart 遇到一个错误。\r\n".encode("gbk"))
    )
    with pytest.raises(DiskLayoutError, match="diskpart 报错"):
        disk_layout.run_diskpart("clean\n")


def test_run_diskpart_error_marker_english(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    _patch_diskpart(
        monkeypatch,
        _FakeCompleted(0, stdout=b"Virtual Disk Service error:\r\naccess is denied.\r\n"),
    )
    with pytest.raises(DiskLayoutError, match="diskpart 报错"):
        disk_layout.run_diskpart("clean\n")


def test_run_diskpart_timeout(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    def boom(args: list[str], **kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd="diskpart", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(DiskLayoutError, match="超时"):
        disk_layout.run_diskpart("clean\n", timeout_s=1)


def test_decode_console_gbk_fallback() -> None:
    """CJK 锚点必须从 OEM 码页输出里活着解出来 (中文 Windows 主战场)。"""
    raw = "卷 1    E   资料  exFAT  主要分区".encode("gbk")
    assert "资料" in disk_layout._decode_console(raw)
    assert disk_layout._decode_console(b"ascii only") == "ascii only"
    # 非 Windows 无 mbcs 编码时回退 gbk, 再不行 errors=replace 兜底不抛
    assert disk_layout._decode_console(b"\xff\xfe\xff") is not None


def test_free_drive_letter_picks_first_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disk_layout, "_existing_letters", lambda: {"C", "D", "E"})
    assert disk_layout._free_drive_letter() == "F"


def test_free_drive_letter_excludes_and_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disk_layout, "_existing_letters", lambda: {"C", "D"})
    assert disk_layout._free_drive_letter(exclude={"E", "F"}) == "G"
    monkeypatch.setattr(disk_layout, "_existing_letters", lambda: set("CDEFGHIJKLMNOPQRSTUVWXYZ"))
    with pytest.raises(DiskLayoutError, match="空闲盘符"):
        disk_layout._free_drive_letter()


def test_letter_of() -> None:
    assert disk_layout._letter_of(Path("E:")) == "E"
    assert disk_layout._letter_of(Path("e:/")) == "E"
    with pytest.raises(DiskLayoutError, match="盘符"):
        disk_layout._letter_of(Path("/Volumes/UDISK"))


def test_viewer_part_size_policy() -> None:
    # 2026-09-22 用户定稿: 1 GiB 底线 —— 分区1是唯一常驻盘符、拔插可见
    # 可读的入口, 余量直接给足
    assert config.VIEWER_PART_MIN_MIB == 1024
    assert disk_layout.viewer_part_size_mib(0) == 1024
    assert disk_layout.viewer_part_size_mib(200 * 1024 * 1024) == 1024  # 小阅览器吃满底线
    # 900 MiB × 1.25 = 1125 → +32 = 1157 (> 1 GiB 底线, 大阅览器仍自动加宽)
    assert disk_layout.viewer_part_size_mib(900 * 1024 * 1024) == 1157
    assert disk_layout.viewer_part_size_mib(100 * 1024 * 1024) <= disk_layout.viewer_part_size_mib(
        900 * 1024 * 1024
    )


# ---------------------------------------------------------------------------
# M2: resolve_disk 交叉验证 / inspect_layout / provision / refresh / finalize
# ---------------------------------------------------------------------------

_DETAIL_DISK_ZH = (
    "SanDisk Ultra USB 3.0\r\n"
    "磁盘 ID: 1A2B3C4D\r\n"
    "类型: USB\r\n"
    "状态: 联机\r\n"
    "  卷 ###  LTR  标签     FS     类型        大小     状态       信息\r\n"
    "  ------  ---  -------  -----  --------  -------  ---------  --------\r\n"
    "  卷 1     E   资料     exFAT  主要分区    29 GB    正常\r\n"
).encode("gbk")


def _patch_resolve_ok(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bus: str = "USB",
    detail: bytes = _DETAIL_DISK_ZH,
    disk: int = 3,
    letter: str = "E",
) -> None:
    """resolve_disk 的三段 PowerShell + 一段 diskpart 全 fake 为一致视图。"""

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "powershell":
            cmd = args[-1]
            if "Get-Partition -DriveLetter" in cmd:
                return _FakeCompleted(
                    0, _ps([{"DiskNumber": disk, "PartitionNumber": 1, "DriveLetter": letter}])
                )
            if "Get-Disk" in cmd:
                return _FakeCompleted(
                    0, _ps({"FriendlyName": "SanDisk Ultra", "Size": 31104958720, "BusType": bus})
                )
            return _FakeCompleted(0, _ps([{"DriveLetter": letter}, {"DriveLetter": None}]))
        assert args[0] == "diskpart", f"意外命令: {args[0]}"
        return _FakeCompleted(0, detail)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_resolve_disk_cross_validation_ok(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    # diskpart detail 的 GBK 中文盘面输出, PowerShell 结构化通道一致指向磁盘 3
    _patch_resolve_ok(monkeypatch)
    s = disk_layout.resolve_disk("E")
    assert s == DiskSummary(
        disk_number=3, model="SanDisk Ultra", size_bytes=31104958720, bus_type="USB", letters=["E"]
    )


def test_resolve_disk_mismatch_refuses(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    # PowerShell 说磁盘 3, diskpart 卷表却只有 F (不含 E) —— 双通道打架, 拒绝
    detail = b"Volume 1     F   DATA   exFAT   Partition   29 GB   Healthy\r\n"
    _patch_resolve_ok(monkeypatch, detail=detail)
    with pytest.raises(DiskLayoutError, match="交叉验证"):
        disk_layout.resolve_disk("E")


def test_resolve_disk_non_usb_refuses(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    _patch_resolve_ok(monkeypatch, bus="SATA")
    with pytest.raises(DiskLayoutError, match="USB 总线"):
        disk_layout.resolve_disk("E")


def test_resolve_disk_powershell_failure_friendly(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        assert args[0] == "powershell"
        return _FakeCompleted(1, stderr=b"Get-Partition : No MSFT_DiskPartition")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DiskLayoutError, match="PowerShell 查询失败"):
        disk_layout.resolve_disk("E")


def _patch_inspect(
    monkeypatch: pytest.MonkeyPatch, style: str | None, parts: list[dict[str, Any]]
) -> None:
    """inspect_layout 的 fake: Get-Disk PartitionStyle + Get-Partition 分区表。"""

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        assert args[0] == "powershell", f"意外命令: {args[0]}"
        cmd = args[-1]
        if "Get-Disk" in cmd:
            return _FakeCompleted(0, _ps({"PartitionStyle": style}))
        assert "Get-Partition -DiskNumber" in cmd
        return _FakeCompleted(0, _ps(parts))

    monkeypatch.setattr(subprocess, "run", fake_run)


def _mbr(p2_type: int | None, p2_letter: str | None) -> list[dict[str, Any]]:
    return [
        {"PartitionNumber": 1, "DriveLetter": "E", "MbrType": 7, "GptType": None},
        {"PartitionNumber": 2, "DriveLetter": p2_letter, "MbrType": p2_type, "GptType": None},
    ]


def _gpt(
    p2_letter: str | None, p2_guid: str | None = config.GPT_BASIC_DATA_TYPE
) -> list[dict[str, Any]]:
    return [
        {
            "PartitionNumber": 1,
            "DriveLetter": "E",
            "MbrType": None,
            "GptType": config.GPT_BASIC_DATA_TYPE,
        },
        {"PartitionNumber": 2, "DriveLetter": p2_letter, "MbrType": None, "GptType": p2_guid},
    ]


def test_inspect_layout_mbr_letterless_0x07_is_dual(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_inspect(monkeypatch, "MBR", _mbr(7, None))
    layout = disk_layout.inspect_layout(3)
    assert layout.dual_scheme == config.DUAL_SCHEME_MBR
    assert layout.is_dual_entry and not layout.legacy_hidden_p2


def test_inspect_layout_mbr_legacy_0x17_is_dual_and_flagged(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """2026-09-16 旧版布局: 0x17 认作 mbr 双入口并标记待迁移 (macOS 不挂 0x17)。"""
    _patch_inspect(monkeypatch, "MBR", _mbr(0x17, None))
    layout = disk_layout.inspect_layout(3)
    assert layout.dual_scheme == config.DUAL_SCHEME_MBR
    assert layout.legacy_hidden_p2


def test_inspect_layout_gpt_letterless_basic_data_is_dual(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_inspect(monkeypatch, "GPT", _gpt(None))
    layout = disk_layout.inspect_layout(3)
    assert layout.dual_scheme == config.DUAL_SCHEME_GPT
    assert not layout.legacy_hidden_p2


def test_inspect_layout_gpt_with_letter_not_dual(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """普通 GPT 双分区 U 盘 (分区2有盘符): 不认, 走清盘重建路径。"""
    _patch_inspect(monkeypatch, "GPT", _gpt("K"))
    assert disk_layout.inspect_layout(3).dual_scheme is None


def test_inspect_layout_gpt_non_basic_data_not_dual(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_inspect(
        monkeypatch, "GPT", _gpt(None, p2_guid="{c12a7328-f81f-11d2}-ba4b-00a0c93ec93b}")
    )
    assert disk_layout.inspect_layout(3).dual_scheme is None


def test_inspect_layout_mbr_lettered_0x07_not_dual(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_inspect(monkeypatch, "MBR", _mbr(7, "K"))
    assert disk_layout.inspect_layout(3).dual_scheme is None


def test_inspect_layout_single_partition_not_dual(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_inspect(
        monkeypatch,
        "MBR",
        [{"PartitionNumber": 1, "DriveLetter": "E", "MbrType": 7, "GptType": None}],
    )
    assert disk_layout.inspect_layout(3).dual_scheme is None


def test_inspect_layout_unknown_style_or_null_fields_not_guessed(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """字段缺失/制式未知: 判 None —— 宁可不识别为双入口, 也不猜。"""
    _patch_inspect(monkeypatch, "GPT", _gpt(None, p2_guid=None))
    layout = disk_layout.inspect_layout(3)
    assert layout.partitions[1].gpt_type is None
    assert layout.dual_scheme is None
    _patch_inspect(monkeypatch, None, _mbr(7, None))
    assert disk_layout.inspect_layout(3).dual_scheme is None
    _patch_inspect(monkeypatch, "IRR", _mbr(7, None))
    assert disk_layout.inspect_layout(3).dual_scheme is None


def test_inspect_layout_zero_partitions_raises(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_inspect(monkeypatch, "MBR", [])
    with pytest.raises(DiskLayoutError, match="查询不到任何分区"):
        disk_layout.inspect_layout(3)


def test_provision_script_shape_and_postverify(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_disk_size_mib", lambda n: 30000)
    monkeypatch.setattr(
        disk_layout,
        "resolve_disk",
        lambda letter: DiskSummary(3, "SanDisk Ultra", 1, "USB", [letter]),
    )
    # 卷盘符轮询结果: 分区1 恰好被挂载管理器自动补上目标盘符 E; 分区2 已
    # 枚举但无盘符 (走 assign)。
    _patch_volume_letters(monkeypatch, "E", "")
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    root = disk_layout.provision_dual_layout(3, "E", 300)
    assert root == Path("K:/")
    # 4 段: ①建两个分区 ②分区1 format (E 已被自动补上, 免 assign)
    # ③分区2 assign ④分区2 format
    assert len(scripts) == 4
    s0, s1, s2, s3 = scripts
    assert (
        s0.index("clean")
        < s0.index("convert mbr")
        < s0.index("convert gpt")
        < s0.index("create partition primary")
    )
    # 2026-09-18 真机回归: clean 后是 RAW 裸盘, Win11 24H2 diskpart 直接
    # convert gpt 报 "not MBR formatted" (0x80070057) —— 必须先 convert
    # mbr 把裸盘初始化成 MBR, 再 convert gpt 翻格式。
    assert s0.count("convert mbr") == 1
    # 2026-09-17 真机回归: 两个分区都必须显式限宽。裸 create 吃满全盘后,
    # 分区2的 create 在零可用空间上失败 (diskpart 0x80070057, 盘停在半重建态)。
    # v2.4.0 反转布局: 分区1 (阅览器, 定宽 300) 在前, 分区2 (加密链,
    # 总量 30000 - 300 - 8 余量) 在后。
    assert "create partition primary size=300" in s0
    assert "create partition primary size=29692" in s0  # 30000 - 300 - 8 余量
    assert s0.index("create partition primary size=300") < s0.index(
        "create partition primary size=29692"
    )
    assert "create partition primary\n" not in s0  # 绝不允许不限宽的裸建分区
    # 2026-09-22 真机回归 (第二轮): 慢棒上 create 后卷未枚举, assign 与
    # format 一样要卷焦点 —— 同一脚本紧跟哪条都报 "There is no volume
    # specified"。必须拆段: create 只建分区, 盘符先经轮询兜住, format 各
    # 自落在已枚举的卷上 (select partition 定焦点)。
    assert "assign" not in s0  # 建分区脚本绝不夹带 assign/format
    assert "format" not in s0
    # 2026-09-28 NTFS 交付形态: 双分区 format 一律 ntfs (防误删 ACL 只在
    # NTFS 上成立; 分区类型字节 0x07 不变, exFAT/NTFS 通用)
    assert s1 == "select disk 3\nselect partition 1\nformat fs=ntfs quick\n"
    assert s2 == "select disk 3\nselect partition 2\nassign letter=K\n"
    assert s3 == "select disk 3\nselect partition 2\nformat fs=ntfs quick\n"
    assert sum(s.count("format fs=ntfs quick") for s in scripts) == 2
    assert not any("exfat" in s for s in scripts)  # 旧 exFAT 形态绝不再产出


def test_provision_replaces_wrong_auto_letter(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """挂载管理器抢先给分区1自动补了别的盘符 (非目标 E): 先摘再指 —— 卷挂
    多个盘符会在资源管理器重复显示, 不能只叠一个 E 上去。"""
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_disk_size_mib", lambda n: 30000)
    monkeypatch.setattr(
        disk_layout, "resolve_disk", lambda letter: DiskSummary(3, "S", 1, "USB", [letter])
    )
    _patch_volume_letters(monkeypatch, "H", "K")
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    disk_layout.provision_dual_layout(3, "E", 300)
    assert "remove letter=H" in scripts[1]
    assert scripts[1].index("remove letter=H") < scripts[1].index("assign letter=E")
    assert "assign" not in scripts[2]  # 分区2 恰好自动补上 K, 免 assign


def test_provision_waits_for_volume_enumeration(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """慢棒竞态 (2026-09-22 真机第二轮): create 后卷对象要过一会儿才枚举
    (Get-Volume 查空) —— 轮询等它出现, 绝不带着未枚举的卷去 assign。"""
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_disk_size_mib", lambda n: 30000)
    monkeypatch.setattr(
        disk_layout, "resolve_disk", lambda letter: DiskSummary(3, "S", 1, "USB", [letter])
    )
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    polls: list[int] = []

    def slow_then_ready(_disk: int, part: int) -> str | None:
        polls.append(part)
        return None if part == 1 and polls.count(1) <= 2 else {1: "E", 2: "K"}[part]

    monkeypatch.setattr(disk_layout, "_partition_volume_letter", slow_then_ready)
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    disk_layout.provision_dual_layout(3, "E", 300)
    assert polls.count(1) == 3  # 前两轮未枚举, 第三轮就绪
    assert sleeps == [disk_layout._VOLUME_WAIT_INTERVAL_S] * 2
    assert len(scripts) == 3  # 两轮等待期间绝没有提前发 format/assign 脚本


def test_provision_volume_never_enumerates_raises(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """轮询超时 (极慢/坏棒): 明确报错并指引重跑自愈, 不发任何 assign/format。"""
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_disk_size_mib", lambda n: 30000)
    _patch_volume_letters(monkeypatch, None, None)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    with pytest.raises(DiskLayoutError, match="卷仍未枚举"):
        disk_layout.provision_dual_layout(3, "E", 300)
    assert len(scripts) == 1  # 只有建分区脚本, 从未 assign/format
    assert len(sleeps) == disk_layout._VOLUME_WAIT_ATTEMPTS


def test_provision_mbr_scheme_converts_mbr(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_disk_size_mib", lambda n: 30000)
    _patch_volume_letters(monkeypatch, "E", "K")
    monkeypatch.setattr(
        disk_layout, "resolve_disk", lambda letter: DiskSummary(3, "S", 1, "USB", [letter])
    )
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    disk_layout.provision_dual_layout(3, "E", 300, scheme=config.DUAL_SCHEME_MBR)
    s = scripts[0]
    assert "convert mbr" in s and "convert gpt" not in s


def test_provision_unknown_scheme_refuses_before_any_script(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    with pytest.raises(DiskLayoutError, match="未知双入口制式"):
        disk_layout.provision_dual_layout(3, "E", 300, scheme="bogus")
    assert scripts == []


def test_provision_postverify_failure_raises(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: False)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_disk_size_mib", lambda n: 30000)
    _patch_volume_letters(monkeypatch, "E", "K")
    _patch_diskpart(monkeypatch, _FakeCompleted(0))
    with pytest.raises(DiskLayoutError, match="未挂载"):
        disk_layout.provision_dual_layout(3, "E", 300)


def test_provision_letter_drift_refuses(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    """clean 后原盘符漂到别的设备 (极小概率): 复核不过, 宁可拒绝写入。"""
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_disk_size_mib", lambda n: 30000)
    _patch_volume_letters(monkeypatch, "E", "K")
    _patch_diskpart(monkeypatch, _FakeCompleted(0))
    monkeypatch.setattr(
        disk_layout, "resolve_disk", lambda letter: DiskSummary(9, "别的盘", 1, "USB", [letter])
    )
    with pytest.raises(DiskLayoutError, match="指向了磁盘"):
        disk_layout.provision_dual_layout(3, "E", 300)


def test_disk_size_mib_queries_and_floors(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        assert args[0] == "powershell"
        return _FakeCompleted(0, _ps({"Size": 15_514_864_640}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert disk_layout._disk_size_mib(1) == 14_796  # 15.5GB 盘, MiB 向下取整


def test_disk_size_mib_refuses_unknown(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    """容量查不到/为 0: 拒绝 —— 绝不在未知几何下 clean。"""
    replies = [_FakeCompleted(0, _ps([])), _FakeCompleted(0, _ps({"Size": None}))]
    queue = list(replies)

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        assert args[0] == "powershell"
        return queue.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DiskLayoutError, match="无返回"):
        disk_layout._disk_size_mib(1)
    with pytest.raises(DiskLayoutError, match="容量异常"):
        disk_layout._disk_size_mib(1)


def test_provision_too_small_disk_refuses_before_clean(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """盘容量装不下双分区: 在下发任何 diskpart 脚本之前就拒绝 (零破坏)。"""
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_disk_size_mib", lambda n: 300)
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    with pytest.raises(DiskLayoutError, match="装不下"):
        disk_layout.provision_dual_layout(3, "E", 300)
    assert scripts == []  # clean 从未执行


def test_refresh_reuses_existing_letter(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    """上次 finalize 失败残留的分区2盘符: 直接复用, 不再 assign。"""

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        assert args[0] == "powershell"
        return _FakeCompleted(0, _ps([{"DriveLetter": "K"}]))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert disk_layout.refresh_partition2(3) == Path("K:/")


def test_refresh_assigns_when_no_letter(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    launched: list[str] = []

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "powershell":
            cmd = args[-1]
            if "Get-Disk" in cmd:
                return _FakeCompleted(0, _ps({"PartitionStyle": "GPT"}))
            return _FakeCompleted(0, _ps([{"DriveLetter": None}]))
        launched.append(Path(args[2]).read_text(encoding="ascii"))
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)
    assert disk_layout.refresh_partition2(3) == Path("K:/")
    # 2026-09-22 真机回归: 已交付盘分区2带 GPT 无盘符属性, 清属性必须在
    # assign 之前 —— 直接 assign 挂不出卷根, 复烧报"分区2未能挂载"中止。
    assert launched == [
        "select disk 3\nselect partition 2\ngpt attributes=0x0000000000000000\nassign letter=K\n"
    ]


def test_refresh_migrates_legacy_0x17_before_assign(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """旧版 0x17 盘: set id=0x07 必须发生在 assign letter 之前 —— Windows 对
    隐藏类型不挂文件系统, 先 assign 只会挂出"没有卷根"的假失败。"""
    launched: list[str] = []

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "powershell":
            cmd = args[-1]
            if "Get-Disk" in cmd:
                return _FakeCompleted(0, _ps({"PartitionStyle": "MBR"}))
            return _FakeCompleted(0, _ps([{"DriveLetter": None}]))
        launched.append(Path(args[2]).read_text(encoding="ascii"))
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(disk_layout, "_free_drive_letter", lambda exclude=None: "K")
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)
    assert disk_layout.refresh_partition2(3, migrate_legacy_mbr=True) == Path("K:/")
    assert len(launched) == 2
    assert launched[0] == (
        f"select disk 3\nselect partition 2\nset id={config.PART2_TYPE_EXFAT:#04x} override\n"
    )
    assert launched[1] == "select disk 3\nselect partition 2\nassign letter=K\n"


_GPT_ATTR_DETAIL = b"Partition 2\r\nType: ebd0a0a2-...\r\nGPT Attributes: 0x8000000000000000\r\n"


def test_finalize_gpt_sets_attribute_and_postverifies(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: False)
    scripts, _ = _patch_diskpart(
        monkeypatch, [_FakeCompleted(0), _FakeCompleted(0, stdout=_GPT_ATTR_DETAIL)]
    )
    disk_layout.finalize_partition2(3, "K")  # 默认 gpt
    s = scripts[0]
    assert "select disk 3" in s and "select partition 2" in s
    assert s.index("remove letter=K") < s.index(
        f"gpt attributes={config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER:#018x}"
    )
    assert "set id=" not in s
    assert "detail partition" in scripts[1]  # 属性位语义后验


def test_finalize_gpt_attribute_missing_raises(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """属性没落上 = 换台 Windows 分区2就现身: 语义后验不过, 坏盘不出仓。"""
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: False)
    _patch_diskpart(monkeypatch, [_FakeCompleted(0), _FakeCompleted(0, stdout=b"Partition 2\r\n")])
    with pytest.raises(DiskLayoutError, match="无盘符属性未生效"):
        disk_layout.finalize_partition2(3, "K", scheme=config.DUAL_SCHEME_GPT)


def test_finalize_mbr_script_shape(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: False)
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    disk_layout.finalize_partition2(3, "K", scheme=config.DUAL_SCHEME_MBR)
    s = scripts[0]
    assert s.index("remove letter=K") < s.index(f"set id={config.PART2_TYPE_EXFAT:#04x} override")
    assert "gpt attributes" not in s
    assert len(scripts) == 1  # mbr 无属性位可后验, 不发 detail


def test_finalize_unknown_scheme_refuses(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    scripts, _ = _patch_diskpart(monkeypatch, _FakeCompleted(0))
    with pytest.raises(DiskLayoutError, match="未知双入口制式"):
        disk_layout.finalize_partition2(3, "K", scheme="bogus")
    assert scripts == []


def test_finalize_postverify_failure_raises(monkeypatch: pytest.MonkeyPatch, win32: None) -> None:
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: True)  # 盘符没摘掉
    _patch_diskpart(monkeypatch, _FakeCompleted(0))
    with pytest.raises(DiskLayoutError, match="未能摘除"):
        disk_layout.finalize_partition2(3, "K", scheme=config.DUAL_SCHEME_MBR)


# ---- 跨平台自愈 (2026-09-29): partition2_state / heal_partition2_hidden ----
# 实机定案: macOS 改写 GPT 会剥掉微软专属的无盘符属性位, 收件 Windows
# 下次插入就给分区2补盘符 —— verify 读态报警, heal 免重烧恢复。

_PS_P2 = {"PartitionNumber": 2, "DriveLetter": "K", "GptType": "ebd0a0a2-b9e5-4433-87c0-68b6a26612ad",
          "GptAttributes": "0x0000000000000000"}


def _patch_ps(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    monkeypatch.setattr(
        disk_layout, "_powershell_json", lambda cmd, **kw: payload
        if isinstance(payload, list) else [payload])


def test_partition2_state_reads_letter_and_attribute(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """被 macOS 剥过属性的现场: 盘符 K 在挂 + 属性位为 0 —— 两项异常都要报。"""
    _patch_ps(monkeypatch, dict(_PS_P2))
    state = disk_layout.partition2_state(3)
    assert state == {"letters": ["K"], "is_gpt": True, "attr_ok": False}


def test_partition2_state_healthy_dual(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_ps(monkeypatch, {**_PS_P2, "DriveLetter": None,
                            "GptAttributes": "0x8000000000000000"})
    state = disk_layout.partition2_state(3)
    assert state == {"letters": [], "is_gpt": True, "attr_ok": True}


def test_partition2_state_mbr_marks_inapplicable(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_ps(monkeypatch, {"PartitionNumber": 2, "DriveLetter": None,
                            "GptType": None, "GptAttributes": "0x0000000000000000"})
    state = disk_layout.partition2_state(3)
    assert state == {"letters": [], "is_gpt": False, "attr_ok": False}


def test_partition2_state_single_partition_returns_none(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_ps(monkeypatch, [{"PartitionNumber": 1, "DriveLetter": "E", "GptType": "ebd0",
                             "GptAttributes": "0x0000000000000000"}])
    assert disk_layout.partition2_state(3) is None


def test_disk_number_of_requires_unique_disk(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_ps(monkeypatch, [{"DiskNumber": 3}])
    assert disk_layout.disk_number_of("E") == 3
    _patch_ps(monkeypatch, [])
    with pytest.raises(DiskLayoutError, match="无法唯一确定磁盘"):
        disk_layout.disk_number_of("E")


def test_heal_removes_letter_and_reapplies_attribute(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """自愈主路径: 摘掉被补挂的盘符 → 重落属性位 → detail 读回后验。"""
    launched: list[str] = []

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "powershell":
            return _FakeCompleted(0, _ps([dict(_PS_P2)]))
        launched.append(Path(args[2]).read_text(encoding="ascii"))
        return _FakeCompleted(0, stdout=_GPT_ATTR_DETAIL)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(disk_layout, "_root_exists", lambda letter: False)
    assert disk_layout.heal_partition2_hidden(3) == ["K"]
    assert launched == [
        "select disk 3\nselect partition 2\nremove letter=K\n"
        f"gpt attributes={config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER:#018x}\n",
        "select disk 3\nselect partition 2\ndetail partition\n",
    ]


def test_heal_without_letters_only_sets_attribute(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """属性被剥但盘符还没被补 (未插过 Windows): 只落属性位, 盘符段为空。"""
    launched: list[str] = []

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "powershell":
            return _FakeCompleted(0, _ps([{**_PS_P2, "DriveLetter": None}]))
        launched.append(Path(args[2]).read_text(encoding="ascii"))
        return _FakeCompleted(0, stdout=_GPT_ATTR_DETAIL)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert disk_layout.heal_partition2_hidden(3) == []
    assert launched[0] == (
        "select disk 3\nselect partition 2\n"
        f"gpt attributes={config.PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER:#018x}\n"
    )


def test_heal_refuses_single_partition_and_mbr(
    monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    _patch_ps(monkeypatch, [{"PartitionNumber": 1, "DriveLetter": "E"}])
    with pytest.raises(DiskLayoutError, match="没有分区2"):
        disk_layout.heal_partition2_hidden(3)
    _patch_ps(monkeypatch, {"PartitionNumber": 2, "DriveLetter": None,
                            "GptType": None, "GptAttributes": "0x0000000000000000"})
    with pytest.raises(DiskLayoutError, match="不是 GPT 制式"):
        disk_layout.heal_partition2_hidden(3)


def test_heal_readback_failure_raises(monkeypatch: pytest.MonkeyPatch, win32: None
) -> None:
    """读回后验不过 = 属性没真落上 (换台 Windows 分区2就现身): 坏盘不出仓。"""
    launched: list[str] = []

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "powershell":
            return _FakeCompleted(0, _ps([{**_PS_P2, "DriveLetter": None}]))
        launched.append(Path(args[2]).read_text(encoding="ascii"))
        return _FakeCompleted(0, stdout=b"Partition 2\r\n")  # 无属性行

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DiskLayoutError, match="无盘符属性未生效"):
        disk_layout.heal_partition2_hidden(3)
    assert "gpt attributes" in launched[0]  # 确已尝试落属性才判读回
