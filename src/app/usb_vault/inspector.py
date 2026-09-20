"""盘读取与校验: burner 烧录后自检、CLI verify/inspect、viewer 启动共用。

异常分级 (viewer 据此给出准确提示):
- NotAVaultError   盘上没有 usb_vault 产物 (keyinfo/manifest 缺失或坏)
- WrongDriveError  结构在但解不开 => 换了盘 / 克隆盘 / 数据被篡改
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from src.app.usb_vault import config, volume_id
from src.app.usb_vault.crypto import ByteReader, CryptoError, decrypt_chunks, derive_data_key
from src.app.usb_vault.unlock_code import UnlockCodeError, derive_component
from src.app.usb_vault.manifest import Manifest, decrypt_manifest
from src.app.usb_vault.volume_id import VolumeIdError, get_volume_serial

SerialGetter = Callable[[Path], int]

KEYINFO_SIZE = 28  # 旧版: magic(8) + license_nonce(16) + crc32(4)
KEYINFO_SIZE_V2 = 29  # v2: + mode(1), mode=0x01 表示需要解锁码
_KEYINFO_MODE_PASSWORD = 0x01


class InspectorError(Exception):
    """盘结构读取失败。"""


class NotAVaultError(InspectorError):
    """该盘不是 usb_vault 资料盘 (缺少/损坏关键文件)。"""


class WrongDriveError(InspectorError):
    """解密失败: 不是授权的那只 U 盘、解锁码错误, 或数据被篡改。"""


class PasswordRequiredError(InspectorError):
    """此盘为密码模式, 调用方须先取得解锁码再重试 open_manifest。"""


@dataclass(slots=True)
class KeyInfo:
    nonce: bytes  # license_nonce (公开盐)
    password: bool  # True = 需要解锁码 (keyinfo v2)


# ---------------------------------------------------------------------------
# keyinfo.bin
# ---------------------------------------------------------------------------

def build_keyinfo(license_nonce: bytes, *, password_mode: bool = False) -> bytes:
    """明文盐载体: magic + nonce(16) + mode(1) + CRC32。nonce 是公开盐, 不含秘密。"""
    if len(license_nonce) != 16:
        raise InspectorError(f"license_nonce 长度错误: {len(license_nonce)}")
    mode = bytes([_KEYINFO_MODE_PASSWORD if password_mode else 0x00])
    head = config.MAGIC_KEYINFO + license_nonce + mode
    return head + struct.pack("<I", zlib.crc32(head) & 0xFFFFFFFF)


def read_keyinfo_blob(blob: bytes) -> KeyInfo:
    """解析 keyinfo.bin 字节: 结构/CRC 错误视为非资料盘; 兼容 28B 旧版。"""
    if blob[:8] != config.MAGIC_KEYINFO:
        raise NotAVaultError(f"{config.KEYINFO_NAME} 结构错误")
    if len(blob) == KEYINFO_SIZE:  # 旧版无码盘
        body, nonce, password = blob, blob[8:24], False
    elif len(blob) == KEYINFO_SIZE_V2:
        mode = blob[24]
        if mode not in (0x00, _KEYINFO_MODE_PASSWORD):
            raise NotAVaultError(f"{config.KEYINFO_NAME} mode 非法: {mode:#04x}")
        body, nonce, password = blob, blob[8:24], mode == _KEYINFO_MODE_PASSWORD
    else:
        raise NotAVaultError(f"{config.KEYINFO_NAME} 结构错误")
    crc = struct.unpack("<I", body[-4:])[0]
    if crc != (zlib.crc32(body[:-4]) & 0xFFFFFFFF):
        raise NotAVaultError(f"{config.KEYINFO_NAME} CRC 校验失败")
    return KeyInfo(nonce=nonce, password=password)


def read_keyinfo(drive_root: Path) -> KeyInfo:
    """从盘根读出 keyinfo。"""
    try:
        blob = (drive_root / config.KEYINFO_NAME).read_bytes()
    except OSError as exc:
        raise NotAVaultError(f"缺少 {config.KEYINFO_NAME}: 该盘不是资料盘") from exc
    return read_keyinfo_blob(blob)


# ---------------------------------------------------------------------------
# 打开清单
# ---------------------------------------------------------------------------

def _serial_candidates(drive_root: Path, serial_getter: SerialGetter) -> list[int]:
    """首选精确单值; 读取被拒时降级免特权多候选通道 (mac_volume.D2 场景)。

    macOS 收件人 (非管理员双击 .app) 直读 /dev/diskNsM 恒 PermissionError,
    diskutil VolumeUUID 通道由系统守护进程代劳仍可用 —— 候选由清单 GCM
    认证筛选。免特权通道也不可用时, 这里抛出的 VolumeIdError 即最终原因
    (自定义 serial_getter 抛错时同样经 candidates 兜底, Windows 下等价重抛)。
    """
    try:
        return [serial_getter(drive_root)]
    except VolumeIdError:
        return volume_id.get_volume_serial_candidates(drive_root)


def open_manifest(
    drive_root: Path,
    seed: bytes,
    *,
    serial_getter: SerialGetter = get_volume_serial,
    unlock_code: str | None = None,
) -> tuple[Manifest, bytes]:
    """推导密钥并解出清单; 返回 (manifest, data_key)。

    data_key 供调用方继续解密具体文件, 避免重复推导 scrypt。
    密码模式的盘: 未提供码抛 PasswordRequiredError (查看器据此弹输入框),
    码错误/盘不对统一表现为 WrongDriveError (解密层面不可区分, 属预期)。
    序列号多候选 (macOS 免特权降级) 时逐一试解, GCM tag 命中即真值。
    """
    ki = read_keyinfo(drive_root)
    code_bytes: bytes | None = None
    if ki.password:
        if not unlock_code or not unlock_code.strip():
            raise PasswordRequiredError("此盘需要输入解锁码后才能打开")
        try:
            code_bytes = derive_component(unlock_code)
        except UnlockCodeError as exc:
            raise WrongDriveError(f"解锁码格式不正确: {exc}") from exc
    serials = _serial_candidates(drive_root, serial_getter)
    try:
        blob = (drive_root / config.MANIFEST_NAME).read_bytes()
    except OSError as exc:
        raise NotAVaultError(f"缺少 {config.MANIFEST_NAME}: 该盘不是资料盘") from exc
    for serial in serials:
        try:
            data_key = derive_data_key(seed, ki.nonce, serial, unlock_code=code_bytes)
        except CryptoError as exc:
            raise NotAVaultError(f"密钥推导失败: {exc}") from exc
        try:
            manifest = decrypt_manifest(blob, data_key)
        except CryptoError:
            continue
        return manifest, data_key
    raise WrongDriveError("此盘不是授权U盘 (或数据已损坏): 清单解密失败")


# ---------------------------------------------------------------------------
# 全量校验
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FileVerifyResult:
    rel_path: str
    enc_hash_ok: bool = False
    decrypt_ok: bool = False
    plain_hash_ok: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.enc_hash_ok and self.decrypt_ok \
            and self.plain_hash_ok


@dataclass(slots=True)
class VerifyReport:
    title: str = ""
    results: list[FileVerifyResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def failed(self) -> list[FileVerifyResult]:
        return [r for r in self.results if not r.ok]


def verify_drive(
    drive_root: Path,
    seed: bytes,
    *,
    serial_getter: SerialGetter = get_volume_serial,
    unlock_code: str | None = None,
) -> VerifyReport:
    """全量复检: 逐文件 密文SHA256 -> 解密 -> 明文SHA256。

    整读与流式两趟各自独立打开密文文件。
    """
    manifest, data_key = open_manifest(drive_root, seed, serial_getter=serial_getter,
                                       unlock_code=unlock_code)
    report = VerifyReport(title=manifest.title)
    for entry in manifest.files:
        result = FileVerifyResult(rel_path=entry.rel_path)
        enc_path = drive_root / config.DATA_DIR_NAME / f"{entry.id}.enc"
        try:
            with open(enc_path, "rb") as fh:
                blob = _drain(fh)
        except OSError as exc:
            result.error = f"读取失败: {exc}"
            report.results.append(result)
            continue
        result.enc_hash_ok = hashlib.sha256(blob).hexdigest() == entry.enc_sha256
        try:
            hasher = hashlib.sha256()
            with open(enc_path, "rb") as fh:
                for chunk in decrypt_chunks(data_key, fh):
                    hasher.update(chunk)
            result.decrypt_ok = True
            result.plain_hash_ok = hasher.hexdigest() == entry.sha256
        except CryptoError as exc:
            result.error = str(exc)
        report.results.append(result)
    return report


def _drain(reader: ByteReader) -> bytes:
    """把一个 ByteReader 整读进内存。"""
    buf = bytearray()
    while True:
        chunk = reader.read(config.CHUNK_SIZE)
        if not chunk:
            return bytes(buf)
        buf += chunk
