"""引导扇区解析: 从卷引导扇区 512B 提取卷序列号 (纯函数, 双平台共用)。

跨平台盘身份的根据 (设计文档 D2): exFAT/FAT32/NTFS 的卷序列号存在引导
扇区固定偏移; Windows GetVolumeInformationW 返回的就是这个在盘值, macOS
读分区设备节点前 512 字节后用本模块解析 —— 两个平台物理上读同一份字节,
派生盐一致, 同一只盘双平台可开、克隆盘双平台皆拒。
"""

from __future__ import annotations

import struct

BOOT_SECTOR_SIZE = 512
_SIG_OFFSET = 0x1FE  # 55AA 签名偏移 (FAT 家族与 exFAT 通用)
# MS exFAT 规格 MainBootRegion 字段表: 0x00 JumpBoot[3], 0x03 "EXFAT"[8],
# 0x0B MustBeZero[53] → 0x40 PartitionOffset(8), 0x48 VolumeLength(8),
# 0x50 FatOffset(4), 0x54 FatLength(4), 0x58 ClusterHeapOffset(4),
# 0x5C ClusterCount(4), 0x60 FirstClusterOfRootDirectory(4),
# 0x64 VolumeSerialNumber(4), 0x68 FileSystemRevision(2), ...
# 2026-09-15 修正: 常量曾是 0x67 —— 疑因把 FAT32 序列号的十进制偏移 67
# 误当十六进制搬进 exFAT (0x67 读到的是真序列号末字节+版本号, 恒错)。
_EXFAT_SERIAL_OFFSET = 0x64
_FAT32_SERIAL_OFFSET = 0x43  # FAT32 十进制 67 = 0x43 (勿与 exFAT 混用)
# NTFS BPB: 0x03 OEM "NTFS    "[8], 0x0B BytesPerSector(2), 0x0D SecPerClust(1),
# 0x28 TotalSectors(8), 0x30 MftLcn(8), 0x38 Mft2Lcn(8), 0x40 ClustersPerMft-
# Record(1), 0x48 VolumeSerialNumber(4) ← GetVolumeInformationW 对 NTFS 返回的
# 就是这 4 个字节。NTFS 盘同样支持: Win 端只读属性 + macOS 原生只读挂载。
_NTFS_SERIAL_OFFSET = 0x48

_SIG_55AA = b"\x55\xaa"


class BootSectorError(Exception):
    """引导扇区结构不合法或文件系统不受支持 (文案面向收件人, 分级明确)。"""


def parse_volume_serial(bs: bytes) -> int:
    """解析卷引导扇区, 返回卷序列号 (uint32, 与 Windows API 同值)。

    支持 exFAT/FAT32/NTFS (NTFS 2026-09-15 起支持)。
    抛 BootSectorError: 数据不足 / 坏签名 / 无法识别。
    """
    if len(bs) < BOOT_SECTOR_SIZE:
        raise BootSectorError(f"引导扇区数据不足: {len(bs)} 字节 (需 {BOOT_SECTOR_SIZE})")
    if bs[_SIG_OFFSET:_SIG_OFFSET + 2] != _SIG_55AA:
        raise BootSectorError("引导扇区缺少 55AA 签名 (非 FAT/exFAT/NTFS 卷, 或读取损坏)")
    if bs[0x03:0x08] == b"EXFAT":
        return int(struct.unpack_from("<I", bs, _EXFAT_SERIAL_OFFSET)[0])
    if bs[0x52:0x57] == b"FAT32":
        return int(struct.unpack_from("<I", bs, _FAT32_SERIAL_OFFSET)[0])
    if bs[0x03:0x0B] == b"NTFS    ":
        return int(struct.unpack_from("<I", bs, _NTFS_SERIAL_OFFSET)[0])
    raise BootSectorError("无法识别的文件系统 (支持 exFAT/FAT32/NTFS)")


# ---------------------------------------------------------------------------
# 合成样本 (测试与 mac_volume 自检用)
# ---------------------------------------------------------------------------

def make_exfat_sector(serial: int) -> bytes:
    """构造带指定序列号的 exFAT 引导扇区 (512B)。"""
    bs = bytearray(BOOT_SECTOR_SIZE)
    bs[0x00:0x03] = b"\xeb\x76\x90"
    bs[0x03:0x08] = b"EXFAT"
    struct.pack_into("<I", bs, _EXFAT_SERIAL_OFFSET, serial)
    bs[_SIG_OFFSET:_SIG_OFFSET + 2] = _SIG_55AA
    return bytes(bs)


def make_fat32_sector(serial: int) -> bytes:
    """构造带指定序列号的 FAT32 引导扇区 (512B)。"""
    bs = bytearray(BOOT_SECTOR_SIZE)
    bs[0x52:0x57] = b"FAT32"
    struct.pack_into("<I", bs, _FAT32_SERIAL_OFFSET, serial)
    bs[_SIG_OFFSET:_SIG_OFFSET + 2] = _SIG_55AA
    return bytes(bs)


def make_ntfs_sector(serial: int) -> bytes:
    """构造带指定序列号的 NTFS 引导扇区 (512B)。"""
    bs = bytearray(BOOT_SECTOR_SIZE)
    bs[0x00:0x03] = b"\xeb\x52\x90"
    bs[0x03:0x0B] = b"NTFS    "
    struct.pack_into("<H", bs, 0x0B, 512)   # BytesPerSector
    bs[0x0D] = 1                             # SecPerClust
    struct.pack_into("<Q", bs, 0x28, 61123)  # TotalSectors
    struct.pack_into("<Q", bs, 0x30, 786432)  # MftLcn
    struct.pack_into("<Q", bs, 0x38, 786432 * 2)  # Mft2Lcn
    bs[0x40] = 246                           # ClustersPerMftRecord (补码)
    struct.pack_into("<I", bs, _NTFS_SERIAL_OFFSET, serial)
    bs[_SIG_OFFSET:_SIG_OFFSET + 2] = _SIG_55AA
    return bytes(bs)
