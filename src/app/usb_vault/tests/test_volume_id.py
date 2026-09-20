"""volume_id 单测: 归一化/端序/卷根判定; 真实 API 仅打 C 盘 (永远存在)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.app.usb_vault import volume_id


def test_serial_to_be_valid() -> None:
    assert volume_id.serial_to_be(0x11223344) == b"\x11\x22\x33\x44"
    assert volume_id.serial_to_be(0) == b"\x00\x00\x00\x00"
    assert volume_id.serial_to_be(0xFFFFFFFF) == b"\xff\xff\xff\xff"


@pytest.mark.parametrize("bad", [-1, 0x1_0000_0000])
def test_serial_to_be_invalid(bad: int) -> None:
    with pytest.raises(volume_id.VolumeIdError):
        volume_id.serial_to_be(bad)


def test_volume_root_normalization() -> None:
    bs = chr(92)
    assert volume_id._volume_root("E:") == "E:" + bs
    assert volume_id._volume_root("E:/") == "E:" + bs
    assert volume_id._volume_root(Path("E:/资料")) == "E:" + bs
    assert volume_id._volume_root("C:\\") == "C:" + bs


def test_volume_root_rejects_relative() -> None:
    with pytest.raises(volume_id.VolumeIdError):
        volume_id._volume_root("relative/path")


def test_is_volume_root(tmp_path: Path) -> None:
    assert volume_id.is_volume_root(Path("C:/")) is True
    assert volume_id.is_volume_root(Path("E:/")) is True
    assert volume_id.is_volume_root(tmp_path) is False


def test_get_volume_serial_c() -> None:
    serial = volume_id.get_volume_serial("C:/")
    assert 0 <= serial <= 0xFFFFFFFF


def test_get_volume_fs_c() -> None:
    assert volume_id.get_volume_fs("C:/").upper() in {"NTFS", "FAT32", "EXFAT"}
