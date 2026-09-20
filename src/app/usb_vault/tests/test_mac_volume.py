"""mac_volume 单测: monkeypatch diskutil 与设备读取, 零真硬件依赖。

本机是 Windows 也照测 —— get_volume_serial_mac 的平台门控被绕开
(直接测内部函数组合), 保证 macOS 分支的逻辑在 CI 上被验证。
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from src.app.usb_vault import boot_sector, mac_volume
from src.app.usb_vault.volume_id import VolumeIdError

# 注: mac_volume.sys/.subprocess 与 volume_id.sys 即全局模块对象, 直接 patch 等价
SERIAL = 0x11223344


class _FakeCompleted:
    def __init__(self, rc: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _plist_stdout(ident: str) -> bytes:
    return plistlib.dumps({"DeviceIdentifier": ident, "VolumeName": "UDISK"})


def test_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, _plist_stdout("disk4s1")),
    )
    monkeypatch.setattr(
        mac_volume, "read_boot_sector",
        lambda dev: boot_sector.make_exfat_sector(SERIAL),
    )
    assert mac_volume.get_volume_serial_mac("/Volumes/UDISK") == SERIAL


def test_diskutil_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise OSError("no diskutil")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(VolumeIdError, match="diskutil"):
        mac_volume._device_identifier(Path("/Volumes/UDISK"))


def test_diskutil_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """diskutil 与 mount 双双不识 (真拔盘): 合并诊断报错, 带 diskutil 原始 stderr。"""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stderr=b"Could not find disk"),
    )
    with pytest.raises(VolumeIdError, match="拔出|挂载"):
        mac_volume._device_identifier(Path("/Volumes/UDISK"))


def test_diskutil_rejected_mount_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09 真机: ls 看得到卷但 diskutil 返回非零 —— 解析 `mount` 输出兜底。"""
    mount_out = (b"/dev/disk1s1 on / (apfs, local)\n"
                 b"/dev/disk4s1 on /Volumes/UDISK (exfat, local, nodev, noowners)\n")

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "diskutil":
            return _FakeCompleted(1, stderr=b"not recognized")
        return _FakeCompleted(0, mount_out)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        mac_volume, "read_boot_sector",
        lambda dev: boot_sector.make_exfat_sector(SERIAL),
    )
    assert mac_volume.get_volume_serial_mac("/Volumes/UDISK") == SERIAL


def test_mount_fallback_name_with_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """盘名带空格: mount 行里挂载点含空格, 须完整匹配而不是只取首词。"""
    mount_out = b"/dev/disk2s1 on /Volumes/My USB (exfat, local, noowners)\n"
    captured: list[str] = []

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        captured.append(args[0])
        if args[0] == "diskutil":
            return _FakeCompleted(1)
        return _FakeCompleted(0, mount_out)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mac_volume._device_identifier(Path("/Volumes/My USB")) == "disk2s1"


def test_diskutil_no_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc=0 但缺 DeviceIdentifier: 也走 mount 兜底, 兜底失败才报错。"""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, plistlib.dumps({"VolumeName": "X"})),
    )
    with pytest.raises(VolumeIdError, match="无法定位U盘设备节点"):
        mac_volume._device_identifier(Path("/Volumes/UDISK"))


def test_permission_denied_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from typing import Any

    real_open = open

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if str(file).startswith("/dev/"):
            raise PermissionError(13, "Permission denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(VolumeIdError, match="权限"):
        mac_volume.read_boot_sector("disk4s1")


def test_read_boot_sector_raw_node_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-17 真机: 挂载卷的块节点被 EBUSY 拒 → raw 节点读成功即返回。"""
    import io
    from typing import Any

    opened: list[str] = []

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        if str(file) == "/dev/rdisk4s1":
            return io.BytesIO(boot_sector.make_exfat_sector(0x40E875AA))
        raise AssertionError(f"raw 节点可用时不应再碰块节点: {file}")

    monkeypatch.setattr("builtins.open", fake_open)
    bs = mac_volume.read_boot_sector("disk4s1")
    assert boot_sector.parse_volume_serial(bs) == 0x40E875AA
    assert opened == ["/dev/rdisk4s1"]


def test_read_boot_sector_raw_missing_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """raw 节点打不开 (不存在/被拒等 OSError) → 退回块节点仍能读。"""
    import io
    from typing import Any

    real_open = open

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if str(file) == "/dev/rdisk4s1":
            raise FileNotFoundError(2, "No such file or directory")
        if str(file) == "/dev/disk4s1":
            return io.BytesIO(boot_sector.make_exfat_sector(SERIAL))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert boot_sector.parse_volume_serial(mac_volume.read_boot_sector("disk4s1")) == SERIAL


def test_ntfs_drive_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """NTFS 盘可读序列号 (2026-09-15 起: mac 原生只读挂载 NTFS, 查看器支持)。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, _plist_stdout("disk4s1")),
    )
    monkeypatch.setattr(mac_volume, "read_boot_sector",
                        lambda dev: boot_sector.make_ntfs_sector(SERIAL))
    assert mac_volume.get_volume_serial_mac("/Volumes/UDISK") == SERIAL


def test_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(VolumeIdError, match="仅支持 macOS"):
        mac_volume.get_volume_serial_mac("E:")


def test_volume_id_dispatch_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """volume_id.get_volume_serial 在 darwin 下应转到 mac_volume。"""
    from src.app.usb_vault import volume_id

    monkeypatch.setattr(sys, "platform", "darwin")
    called: dict[str, object] = {}

    def fake_mac(root: str) -> int:
        called["root"] = root
        return SERIAL

    import src.app.usb_vault.mac_volume as mv

    monkeypatch.setattr(mv, "get_volume_serial_mac", fake_mac)
    # 延迟导入按模块属性解析, 打补丁在真实模块上即可生效
    assert volume_id.get_volume_serial("/Volumes/UDISK") == SERIAL
    assert called["root"] == "/Volumes/UDISK"


def test_volume_id_dispatch_unknown_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault import volume_id

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(VolumeIdError, match="不支持的平台"):
        volume_id.get_volume_serial("/media/udisk")


def test_is_volume_root_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault import volume_id

    monkeypatch.setattr(sys, "platform", "darwin")
    assert volume_id.is_volume_root("/Volumes/UDISK")
    assert not volume_id.is_volume_root("/Volumes/UDISK/sub")
    assert not volume_id.is_volume_root("/Users/x")


# ---------------------------------------------------------------------------
# diskutil VolumeUUID 免特权降级 (2026-09 真机: 收件人直读 /dev 恒 EPERM)
# ---------------------------------------------------------------------------

def test_uuid_serial_candidates_forms() -> None:
    """4-4 形态 → 两候选 (半序无文档, GCM 裁决); 全 UUID/空 → 不可用。"""
    assert mac_volume.uuid_serial_candidates("40E8-75AA") == [0x40E875AA, 0x75AA40E8]
    assert mac_volume.uuid_serial_candidates("75aa-40e8") == [0x75AA40E8, 0x40E875AA]
    assert mac_volume.uuid_serial_candidates("ABCD-ABCD") == [0xABCDABCD]
    assert mac_volume.uuid_serial_candidates(
        "12345678-1234-1234-1234-123456789ABC") == []  # 128 位 = 非序列号合成
    assert mac_volume.uuid_serial_candidates(None) == []
    assert mac_volume.uuid_serial_candidates("") == []


def _deny_boot_sector(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟真机 EPERM: read_boot_sector 已把 PermissionError 转成 VolumeIdError。"""
    def denied(_dev: str) -> bytes:
        raise VolumeIdError("读取U盘引导扇区权限不足: 请重新插拔U盘后重试")
    monkeypatch.setattr(mac_volume, "read_boot_sector", denied)


def test_candidates_direct_read_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """直读可用时精确单候选, 不碰 UUID 通道 (烧录语义)。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, _plist_stdout("disk4s1")),
    )
    monkeypatch.setattr(
        mac_volume, "read_boot_sector",
        lambda dev: boot_sector.make_exfat_sector(SERIAL),
    )
    assert mac_volume.get_volume_serial_candidates_mac("/Volumes/UDISK") == [SERIAL]


def test_candidates_fall_back_to_diskutil_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09 真机场景: /dev 被拒 → diskutil UUID (免特权) 给出两候选。"""
    from src.app.usb_vault import volume_id

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, plistlib.dumps(
            {"DeviceIdentifier": "disk2s1", "VolumeUUID": "3344-1122"})),
    )
    _deny_boot_sector(monkeypatch)
    cands = volume_id.get_volume_serial_candidates("/Volumes/UDISK")
    assert cands == [0x33441122, 0x11223344]


def test_candidates_uuid_not_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    """VolumeUUID 是 128 位全形态 (非序列号合成): 拒用, 报错带双通道原因。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, plistlib.dumps(
            {"DeviceIdentifier": "disk2s1",
             "VolumeUUID": "12345678-1234-1234-1234-123456789ABC"})),
    )
    _deny_boot_sector(monkeypatch)
    with pytest.raises(VolumeIdError, match="非序列号合成"):
        mac_volume.get_volume_serial_candidates_mac("/Volumes/UDISK")


def test_candidates_all_channels_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """diskutil 也不给 VolumeUUID: 报错带直读原因 + 具体缺键说明与键名样本。

    2026-09-15 真机: exFAT 的 diskutil 信息里根本没有 VolumeUUID 键 ——
    键名样本进文案, 收件人截图即可自诊断。
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, _plist_stdout("disk2s1")),
    )
    _deny_boot_sector(monkeypatch)
    with pytest.raises(VolumeIdError, match="没有 VolumeUUID 键"):
        mac_volume.get_volume_serial_candidates_mac("/Volumes/UDISK")


def test_diskutil_uuid_note_nonzero_rc(monkeypatch: pytest.MonkeyPatch) -> None:
    """diskutil 退出码非零: note 带退出码与 stderr 摘录。"""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stderr=b"Could not find disk"),
    )
    uuid, note = mac_volume._diskutil_volume_uuid(Path("/Volumes/UDISK"))
    assert uuid is None
    assert "退出码 1" in note
    assert "Could not find disk" in note


# ---------------------------------------------------------------------------
# 管理员授权直读 (sudo -S 载体) —— 2026-09-18 四连探针判死 osascript 后的定案
# ---------------------------------------------------------------------------

def _od_stdout(sector: bytes) -> bytes:
    """root shell 执行 `dd | od -An -tx1 -v` 的回传形态: 空格分隔十六进制。"""
    return " ".join(f"{b:02x}" for b in sector).encode()


def test_admin_read_serial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """授权成功: sudo -S (密码走 stdin) + od 十六进制 + DD_RC=0 → 精确单值。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    sector = boot_sector.make_exfat_sector(0x40E875AA)
    seen: dict[str, object] = {}

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "diskutil":
            return _FakeCompleted(0, _plist_stdout("disk2s1"))
        assert args[0] == "sudo", f"意外命令: {args[0]}"
        assert args[1:3] == ["-S", "-p"], "密码必须走 stdin (-S) 且提示置空"
        seen["input"] = kw.get("input")
        return _FakeCompleted(0, _od_stdout(sector) + b"\nDD_RC=0\nDD_ERR=\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mac_volume.admin_read_serial("/Volumes/TEST1", "pw123") == 0x40E875AA
    assert seen["input"] == b"pw123\n"  # 密码只进 stdin, 不进 argv


def test_admin_read_wrong_password_raises(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """密码不对: sudo rc=1 + "incorrect password" → AdminAuthRejected,
    由查看器层重问; 密码类错误与设备类失败绝不混为一谈。"""
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "diskutil":
            return _FakeCompleted(0, _plist_stdout("disk2s1"))
        return _FakeCompleted(1, stderr=b"sudo: 1 incorrect password attempt")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from src.app.usb_vault.mac_volume import AdminAuthRejected
    with pytest.raises(AdminAuthRejected, match="incorrect password"):
        mac_volume.admin_read_serial("/Volumes/TEST1", "bad-pw")


def test_admin_read_garbage_output(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """sudo 回传不是 512 字节扇区: 解析失败 → None, 原因落 stderr。"""
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "diskutil":
            return _FakeCompleted(0, _plist_stdout("disk2s1"))
        return _FakeCompleted(0, b"00 01 02")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mac_volume.admin_read_serial("/Volumes/TEST1", "pw") is None
    assert "[admin_read_serial]" in capsys.readouterr().err


def test_admin_read_dd_error_surfaced(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """root 下 dd 本身失败 (v2.2.1 真机"授权成功但 0 字节"案): DD_RC/DD_ERR
    自述行把错误原文带回 stderr —— 绝不再让 2>/dev/null 把失败伪装成成功。
    """
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        if args[0] == "diskutil":
            return _FakeCompleted(0, _plist_stdout("disk2s1"))
        return _FakeCompleted(
            0, b"\nDD_RC=1\nDD_ERR=dd: /dev/rdisk2s1: Operation not permitted\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mac_volume.admin_read_serial("/Volumes/TEST1", "pw") is None
    err = capsys.readouterr().err
    assert "[admin_read_serial] root 下 dd 仍失败 (退出码 1)" in err
    assert "Operation not permitted" in err
    assert "完全磁盘访问" in err  # TCC 门后的场合: 指引自开权限


def test_admin_read_rejects_unsafe_ident(monkeypatch: pytest.MonkeyPatch) -> None:
    """设备标识符含 shell 元字符: 白名单校验拒绝, sudo 根本不执行。

    ident 来自 diskutil 正常情况下是 diskNsM, 但通道设计必须假设最坏输入
    —— 校验失败宁可返回 None, 也不把字符串拼进 shell。
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    launched: list[str] = []

    def fake_run(args: list[str], **kw: object) -> _FakeCompleted:
        launched.append(args[0])
        if args[0] == "diskutil":
            return _FakeCompleted(0, plistlib.dumps(
                {"DeviceIdentifier": "disk2; rm -rf ~"}))
        raise AssertionError("unsafe ident 绝不该走到 sudo")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mac_volume.admin_read_serial("/Volumes/TEST1", "pw") is None
    assert launched == ["diskutil"]


def test_admin_read_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 darwin (本 CI 即 win32): 直接 None, 不碰任何子进程。"""
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("win32 下不应启动任何子进程")

    monkeypatch.setattr(subprocess, "run", boom)
    assert mac_volume.admin_read_serial("E:", "pw") is None
