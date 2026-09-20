"""inspector 单测: keyinfo 解析 / 异常分级 / 篡改检测 / 全量复检。

依赖 test_burner 的烧录环境搭建手法 (注入序列号 + tmp 目录)。
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.app.usb_vault import config, inspector
from src.app.usb_vault.burner import BurnOptions, burn

# 隔离开发机真实 dist/ (conftest.dist_isolated), 防止 burn 捡到真 .app
pytestmark = pytest.mark.usefixtures("dist_isolated")

SERIAL = 0x11223344


def _serial(_p: Path) -> int:
    return SERIAL

_SEED: dict[str, bytes] = {}


@pytest.fixture()
def vault(tmp_path: Path, dist_isolated: None) -> Path:
    # dist_isolated 显式依赖: 夹具阶段就 burn, usefixtures 顺序不保证先隔离
    # (开发机 dist/ 放回真 .app 时曾整文件 setup 报错)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("内容A", encoding="utf-8")
    (src / "b.txt").write_text("内容B", encoding="utf-8")
    drive = tmp_path / "USB"
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    seed = os.urandom(64)
    _SEED["s"] = seed
    burn(src, drive, seed,
         BurnOptions(require_root=False, viewer_exe=viewer, convert_office=False,
                     skip_scan=True),
         serial_getter=_serial)
    _make_writable(drive)  # 便于测试中篡改
    return drive


def _make_writable(root: Path) -> None:
    for dirpath, _d, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                os.chmod(p, stat.S_IWUSR | stat.S_IRUSR)
            except OSError:
                pass
            if sys.platform == "win32":
                # 烧录给密文载体打了 +H+S; open('wb') 对已存在的 hidden/system
                # 文件直接 PermissionError (CreateFile CREATE_ALWAYS 语义),
                # 篡改用例模拟攻击者先剥掉全部属性
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(p, 0x20)  # 仅留 ARCHIVE


@pytest.fixture(autouse=True)
def _cleanup(vault: Path) -> Iterator[None]:
    yield
    _make_writable(vault)


def test_open_manifest_ok(vault: Path) -> None:
    m, key = inspector.open_manifest(vault, _SEED["s"], serial_getter=_serial)
    assert len(m.files) == 2
    assert key  # data_key 返回供继续解密


def test_missing_keyinfo(tmp_path: Path) -> None:
    with pytest.raises(inspector.NotAVaultError):
        inspector.open_manifest(tmp_path, os.urandom(64), serial_getter=_serial)


def test_corrupt_keyinfo_magic(vault: Path) -> None:
    p = vault / "keyinfo.bin"
    data = bytearray(p.read_bytes())
    data[0] ^= 0xFF
    p.write_bytes(bytes(data))
    with pytest.raises(inspector.NotAVaultError):
        inspector.open_manifest(vault, _SEED["s"], serial_getter=_serial)


def test_keyinfo_bad_crc(vault: Path) -> None:
    p = vault / "keyinfo.bin"
    data = bytearray(p.read_bytes())
    data[8] ^= 0xFF  # 动了 nonce 但没修 CRC
    p.write_bytes(bytes(data))
    with pytest.raises(inspector.NotAVaultError):
        inspector.open_manifest(vault, _SEED["s"], serial_getter=_serial)


def test_missing_manifest(vault: Path) -> None:
    (vault / "manifest.enc").unlink()
    with pytest.raises(inspector.NotAVaultError):
        inspector.open_manifest(vault, _SEED["s"], serial_getter=_serial)


def test_tampered_enc_detected(vault: Path) -> None:
    m, _ = inspector.open_manifest(vault, _SEED["s"], serial_getter=_serial)
    victim = vault / "DATA" / f"{m.files[0].id}.enc"
    data = bytearray(victim.read_bytes())
    data[40] ^= 0xFF  # 动密文区
    victim.write_bytes(bytes(data))

    report = inspector.verify_drive(vault, _SEED["s"], serial_getter=_serial)
    assert not report.all_ok
    assert len(report.failed) == 1
    assert report.failed[0].rel_path == m.files[0].rel_path


def test_missing_enc_detected(vault: Path) -> None:
    m, _ = inspector.open_manifest(vault, _SEED["s"], serial_getter=_serial)
    (vault / "DATA" / f"{m.files[0].id}.enc").unlink()
    report = inspector.verify_drive(vault, _SEED["s"], serial_getter=_serial)
    assert not report.all_ok
    assert "读取失败" in (report.failed[0].error or "")


def test_build_keyinfo_roundtrip() -> None:
    nonce = os.urandom(16)
    blob = inspector.build_keyinfo(nonce)
    assert len(blob) == inspector.KEYINFO_SIZE_V2  # 新烧一律 v2 (mode=0 无码)
    ki = inspector.read_keyinfo_blob(blob)
    assert ki.nonce == nonce
    assert not ki.password


def test_build_keyinfo_legacy_28b_compat() -> None:
    """旧版 28B keyinfo (无 mode 字节) 仍可读, 视为无码盘。"""
    import struct
    import zlib


    nonce = os.urandom(16)
    legacy = config.MAGIC_KEYINFO + nonce
    blob = legacy + struct.pack("<I", zlib.crc32(legacy) & 0xFFFFFFFF)
    assert len(blob) == inspector.KEYINFO_SIZE
    ki = inspector.read_keyinfo_blob(blob)
    assert ki.nonce == nonce
    assert not ki.password


def test_build_keyinfo_password_mode() -> None:
    nonce = os.urandom(16)
    blob = inspector.build_keyinfo(nonce, password_mode=True)
    assert len(blob) == inspector.KEYINFO_SIZE_V2
    ki = inspector.read_keyinfo_blob(blob)
    assert ki.nonce == nonce
    assert ki.password


def test_keyinfo_bad_mode_byte() -> None:
    import struct
    import zlib


    head = config.MAGIC_KEYINFO + os.urandom(16) + b"\x02"  # 非法 mode
    blob = head + struct.pack("<I", zlib.crc32(head) & 0xFFFFFFFF)
    with pytest.raises(inspector.NotAVaultError):
        inspector.read_keyinfo_blob(blob)


def test_build_keyinfo_bad_nonce() -> None:
    with pytest.raises(inspector.InspectorError):
        inspector.build_keyinfo(b"short")


# ---------------------------------------------------------------------------
# 序列号免特权降级 (2026-09 真机: macOS 收件人直读 /dev 恒 EPERM)
# ---------------------------------------------------------------------------

def _deny_serial(_p: Path) -> int:
    from src.app.usb_vault.volume_id import VolumeIdError
    raise VolumeIdError("读取U盘引导扇区权限不足")


def test_open_manifest_serial_candidates_fallback(
        vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单值 getter 被拒 → candidates 免特权通道; 多候选逐一试解,
    GCM tag 命中的那个即真序列号 (假候选在前也必须解得开)。"""
    from src.app.usb_vault import volume_id

    calls: list[Path] = []

    def fake_candidates(root: Path) -> list[int]:
        calls.append(root)
        return [0xDEADBEEF, SERIAL]

    monkeypatch.setattr(volume_id, "get_volume_serial_candidates", fake_candidates)
    m, key = inspector.open_manifest(vault, _SEED["s"], serial_getter=_deny_serial)
    assert len(m.files) == 2
    assert key
    assert calls == [vault]


def test_open_manifest_candidates_all_wrong(
        vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """候选全不命中 → 与码错同型 WrongDriveError (解密层不可区分, 属预期)。"""
    from src.app.usb_vault import volume_id

    monkeypatch.setattr(volume_id, "get_volume_serial_candidates",
                        lambda root: [0x1, 0x2])
    with pytest.raises(inspector.WrongDriveError, match="清单解密失败"):
        inspector.open_manifest(vault, _SEED["s"], serial_getter=_deny_serial)


def test_open_manifest_no_serial_channel(
        vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """双通道全断: VolumeIdError 原样上抛 (查看器据此给出指引文案)。"""
    from src.app.usb_vault import volume_id
    from src.app.usb_vault.volume_id import VolumeIdError

    def no_fallback(_root: Path) -> list[int]:
        raise VolumeIdError("免特权通道 diskutil 未提供 VolumeUUID")

    monkeypatch.setattr(volume_id, "get_volume_serial_candidates", no_fallback)
    with pytest.raises(VolumeIdError, match="免特权"):
        inspector.open_manifest(vault, _SEED["s"], serial_getter=_deny_serial)


def test_verify_drive_direct(vault: Path) -> None:
    report = inspector.verify_drive(vault, _SEED["s"], serial_getter=_serial)
    assert report.all_ok
