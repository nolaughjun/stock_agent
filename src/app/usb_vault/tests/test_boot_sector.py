"""boot_sector 单测: exFAT/FAT32 解析 + 结构错误分级 (纯函数, 不碰硬件)。"""

from __future__ import annotations

import struct

import pytest

from src.app.usb_vault import boot_sector


def test_exfat_roundtrip() -> None:
    serial = 0x11223344
    bs = boot_sector.make_exfat_sector(serial)
    assert len(bs) == 512
    assert boot_sector.parse_volume_serial(bs) == serial


@pytest.mark.parametrize("serial", [0x00000000, 0xFFFFFFFF, 0xAABBCCDD, 12345])
def test_various_serials(serial: int) -> None:
    assert boot_sector.parse_volume_serial(
        boot_sector.make_exfat_sector(serial)) == serial


def test_fat32_roundtrip() -> None:
    serial = 0xCAFEBABE
    assert boot_sector.parse_volume_serial(
        boot_sector.make_fat32_sector(serial)) == serial


def test_exfat_little_endian() -> None:
    """在盘为小端 u32; 与 Windows API 返回的整数值一致。"""
    bs = boot_sector.make_exfat_sector(0x01020304)
    assert bs[0x64:0x68] == bytes([0x04, 0x03, 0x02, 0x01])
    assert struct.unpack("<I", bs[0x64:0x68])[0] == 0x01020304


def test_exfat_real_layout_serial_at_0x64() -> None:
    """按 MS exFAT 规格逐字段构造真实引导扇区: 序列号必须在 0x64 解出。

    回归测试 (2026-09-15): 常量曾是 0x67 —— FAT32 的十进制 67 被误当
    十六进制搬进 exFAT。make/parse 自洽掩盖了它; 真机上即使有权限读到
    引导扇区, 也会解出"真序列号末字节+FileSystemRevision"拼成的垃圾值,
    解密永远失败。本测试不依赖 make_exfat_sector, 独立锚定规格偏移。
    """
    bs = bytearray(512)
    bs[0x00:0x03] = b"\xeb\x76\x90"          # JumpBoot
    bs[0x03:0x0B] = b"EXFAT   "              # FileSystemName[8]
    # 0x0B..0x40 MustBeZero[53] 保持全零
    struct.pack_into("<Q", bs, 0x40, 2048)   # PartitionOffset (扇区)
    struct.pack_into("<Q", bs, 0x48, 61123)  # VolumeLength (扇区)
    struct.pack_into("<I", bs, 0x50, 24)     # FatOffset
    struct.pack_into("<I", bs, 0x54, 4)      # FatLength
    struct.pack_into("<I", bs, 0x58, 128)    # ClusterHeapOffset
    struct.pack_into("<I", bs, 0x5C, 60000)  # ClusterCount
    struct.pack_into("<I", bs, 0x60, 3)      # FirstClusterOfRootDirectory
    struct.pack_into("<I", bs, 0x64, 0x40E875AA)  # VolumeSerialNumber ← 规格 0x64
    struct.pack_into("<H", bs, 0x68, 0x0100)  # FileSystemRevision 01.00
    struct.pack_into("<H", bs, 0x6A, 0x0000)  # VolumeFlags
    bs[0x6C] = 9   # BytesPerSectorShift (512B)
    bs[0x6D] = 4   # SectorsPerClusterShift
    bs[0x6E] = 1   # NumberOfFats
    bs[0x6F] = 0x80  # DriveSelect
    bs[0x1FE:0x200] = b"\x55\xaa"            # BootSignature
    assert boot_sector.parse_volume_serial(bytes(bs)) == 0x40E875AA


def test_truncated() -> None:
    with pytest.raises(boot_sector.BootSectorError, match="不足"):
        boot_sector.parse_volume_serial(b"\x00" * 100)


def test_bad_signature() -> None:
    bs = bytearray(boot_sector.make_exfat_sector(1))
    bs[0x1FE] = 0x00  # 坏 55AA
    with pytest.raises(boot_sector.BootSectorError, match="55AA"):
        boot_sector.parse_volume_serial(bytes(bs))


def test_ntfs_roundtrip() -> None:
    """NTFS 序列号 @0x48 (2026-09-15 起支持)。"""
    serial = 0x40E875AA
    bs = boot_sector.make_ntfs_sector(serial)
    assert len(bs) == 512
    assert boot_sector.parse_volume_serial(bs) == serial


def test_ntfs_real_layout_serial_at_0x48() -> None:
    """按 NTFS BPB 逐字段构造: 序列号必须在 0x48 解出, OEM 名是 "NTFS"+4空格。"""
    bs = bytearray(512)
    bs[0x00:0x03] = b"\xeb\x52\x90"          # JumpBoot
    bs[0x03:0x0B] = b"NTFS    "              # OEM 名 (8B, 含尾部空格)
    struct.pack_into("<H", bs, 0x0B, 512)    # BytesPerSector
    bs[0x0D] = 8                             # SecPerClust
    struct.pack_into("<H", bs, 0x18, 63)     # SectorsPerTrack
    struct.pack_into("<H", bs, 0x1A, 255)    # NumberOfHeads
    struct.pack_into("<Q", bs, 0x28, 12345678)  # TotalSectors (8B, 非 FAT 的 4B)
    struct.pack_into("<Q", bs, 0x30, 20480)  # MftLcn
    struct.pack_into("<Q", bs, 0x38, 40960)  # Mft2Lcn
    bs[0x40] = 246                           # ClustersPerMftRecord
    bs[0x44] = 1                             # ClustersPerIndexBlock
    struct.pack_into("<I", bs, 0x48, 0x11223344)  # VolumeSerialNumber ← 规格 0x48
    struct.pack_into("<I", bs, 0x4C, 0)      # Checksum
    bs[0x1FE:0x200] = b"\x55\xaa"
    assert boot_sector.parse_volume_serial(bytes(bs)) == 0x11223344


def test_unknown_fs() -> None:
    bs = bytearray(512)
    bs[0x1FE:0x200] = b"\x55\xaa"
    with pytest.raises(boot_sector.BootSectorError, match="无法识别"):
        boot_sector.parse_volume_serial(bytes(bs))
