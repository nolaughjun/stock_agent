"""加密核心: 密钥派生 + 统一分块 AES-256-GCM 流格式。

格式设计 (大小文件同一路径, 一套测试):

.ENC 文件 (32B 头 + 块数据):
    magic(8) + file_nonce(12) + nchunks u32BE(4) + plen u64BE(8)
    之后每块: 密文 ‖ 16B GCM tag, 末块可短 (空文件 = 1 个空块)

    nonce_i = file_nonce[0:8] ‖ (u32be(file_nonce[8:12]) XOR i)   # 单射, 块间不重复
    AAD_i   = header[0:32] ‖ BE(i,4) ‖ (末块 ? 0x01 : 0x00)
              => 篡改头/截断/续接/换序都会在对应块的 tag 上失败

blob (manifest.enc, 24B 头):
    magic(8) + nonce(12) + plen u32BE(4), AAD = header[0:24]

子密钥域分离: file/manifest 各自经 HKDF-SHA256 派生, 即便 nonce 意外
跨域复用也不会泄密。
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Iterator
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.app.usb_vault import config
from src.app.usb_vault.volume_id import serial_to_be

_NONCE_SIZE = 12
_U32_MAX = 0xFFFFFFFF


class ByteReader(Protocol):
    """最小读接口: 真实文件与哈希包装器都结构化满足。"""

    def read(self, n: int = -1) -> bytes: ...


class ByteWriter(Protocol):
    """最小写接口。"""

    def write(self, b: bytes) -> int: ...


class CryptoError(Exception):
    """解密失败: 密钥不符 (盘不对) 或数据被篡改/损坏。"""


# ---------------------------------------------------------------------------
# 密钥派生
# ---------------------------------------------------------------------------

def derive_data_key(seed: bytes, license_nonce: bytes, volume_serial: int,
                    *, unlock_code: bytes | None = None) -> bytes:
    """data_key = scrypt(口令, salt=license_nonce ‖ 卷序列号大端, 32B)。

    口令 = SEED (无码模式) 或 解锁码‖SEED (密码模式) —— 密码模式下码与
    SEED 缺一不可, 泄露任意单者都不足以解密。
    用 hashlib.scrypt (标准库, 支持 maxmem 上限); cryptography 库的 Scrypt
    不接受 maxmem 参数。
    """
    if len(seed) != config.SEED_SIZE:
        raise CryptoError(f"SEED 长度错误: {len(seed)} (期望 {config.SEED_SIZE})")
    if len(license_nonce) != 16:
        raise CryptoError(f"license_nonce 长度错误: {len(license_nonce)} (期望 16)")
    password = seed if unlock_code is None else unlock_code + seed
    try:
        return hashlib.scrypt(
            password,
            salt=license_nonce + serial_to_be(volume_serial),
            n=config.SCRYPT_N,
            r=config.SCRYPT_R,
            p=config.SCRYPT_P,
            dklen=config.SCRYPT_DKLEN,
            maxmem=config.SCRYPT_MAXMEM,
        )
    except ValueError as exc:
        raise CryptoError(f"scrypt 密钥派生失败: {exc}") from exc


def _derive_subkey(data_key: bytes, nonce: bytes, info: bytes) -> bytes:
    """HKDF-SHA256 子密钥: 域分离 (file/manifest 各用独立 info)。"""
    if len(data_key) != 32:
        raise CryptoError(f"主密钥长度错误: {len(data_key)} (期望 32)")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce, info=info)
    return hkdf.derive(data_key)


def _chunk_nonce(base: bytes, index: int) -> bytes:
    """块 nonce: 对基准 nonce 低 32 位 XOR 块序号 (单射, 必不重复)。"""
    counter = struct.unpack(">I", base[8:12])[0] ^ index
    return base[0:8] + struct.pack(">I", counter)


def _chunk_aad(header: bytes, index: int, is_last: bool) -> bytes:
    return header + struct.pack(">I", index) + (b"\x01" if is_last else b"\x00")


def _chunk_count(plain_len: int, chunk_size: int) -> int:
    if plain_len < 0:
        raise CryptoError(f"明文长度为负: {plain_len}")
    if chunk_size <= 0:
        raise CryptoError(f"chunk_size 必须为正: {chunk_size}")
    n = max(1, -(-plain_len // chunk_size))  # ceil, 空文件也算 1 块
    if n > _U32_MAX:
        raise CryptoError(f"块数超出 u32: {n}")
    return n


# ---------------------------------------------------------------------------
# 文件流式加解密
# ---------------------------------------------------------------------------

def encrypt_stream(
    data_key: bytes,
    src: ByteReader,
    dst: ByteWriter,
    *,
    plain_len: int,
    chunk_size: int = config.CHUNK_SIZE,
) -> None:
    """把 src (须可重复定位/长度已知 plain_len) 加密写入 dst。"""
    nchunks = _chunk_count(plain_len, chunk_size)
    file_nonce = os.urandom(_NONCE_SIZE)
    header = (
        config.MAGIC_ENCFILE
        + file_nonce
        + struct.pack(">I", nchunks)
        + struct.pack(">Q", plain_len)
    )
    key = _derive_subkey(data_key, file_nonce, config.HKDF_INFO_FILE)
    aes = AESGCM(key)
    dst.write(header)

    written = 0
    for i in range(nchunks):
        want = min(chunk_size, plain_len - written)
        chunk = src.read(want)
        if len(chunk) != want:
            raise CryptoError(f"源数据短于声明长度: 已读 {written + len(chunk)}/{plain_len}")
        nonce_i = _chunk_nonce(file_nonce, i)
        aad_i = _chunk_aad(header, i, is_last=(i == nchunks - 1))
        dst.write(aes.encrypt(nonce_i, chunk, aad_i))
        written += want
    if written != plain_len:  # 防御性: 理论不可达
        raise CryptoError(f"写入长度不符: {written}/{plain_len}")


def decrypt_chunks(
    data_key: bytes,
    src: ByteReader,
    *,
    chunk_size: int = config.CHUNK_SIZE,
) -> Iterator[bytes]:
    """流式解密 .ENC 文件; 任一块 tag 失败抛 CryptoError。

    注意: 这是生成器, 错误在迭代到对应块时才抛出。
    """
    header = src.read(32)
    if len(header) != 32:
        raise CryptoError("文件头不完整 (被截断?)")
    if header[:8] != config.MAGIC_ENCFILE:
        raise CryptoError("不是有效的加密文件 (magic 不符)")
    file_nonce = header[8:20]
    nchunks = struct.unpack(">I", header[20:24])[0]
    plen = struct.unpack(">Q", header[24:32])[0]

    if nchunks != _chunk_count(plen, chunk_size):
        raise CryptoError(f"文件头自洽性失败: nchunks={nchunks} 与 plen={plen} 不匹配")
    key = _derive_subkey(data_key, file_nonce, config.HKDF_INFO_FILE)
    aes = AESGCM(key)

    produced = 0
    for i in range(nchunks):
        last = i == nchunks - 1
        block_len = plen - chunk_size * (nchunks - 1) if last else chunk_size
        blob = src.read(block_len + config.GCM_TAG_SIZE)
        if len(blob) != block_len + config.GCM_TAG_SIZE:
            raise CryptoError(f"第 {i} 块数据不完整 (被截断?)")
        try:
            chunk = aes.decrypt(
                _chunk_nonce(file_nonce, i),
                blob,
                _chunk_aad(header, i, last),
            )
        except InvalidTag as exc:
            raise CryptoError(
                f"第 {i} 块校验失败: 密钥不符 (盘不对) 或数据被篡改"
            ) from exc
        produced += len(chunk)
        yield chunk

    if src.read(1):
        raise CryptoError("文件末尾存在多余数据 (被续接?)")
    if produced != plen:  # 防御性
        raise CryptoError(f"解密总长不符: {produced}/{plen}")


# ---------------------------------------------------------------------------
# 整块加解密 (manifest)
# ---------------------------------------------------------------------------

def encrypt_blob(
    data_key: bytes,
    plaintext: bytes,
    *,
    magic: bytes,
    info: bytes,
    max_plain: int = config.MANIFEST_MAX_PLAINTEXT,
) -> bytes:
    nonce = os.urandom(_NONCE_SIZE)
    header = magic + nonce + struct.pack(">I", len(plaintext))
    if len(plaintext) > max_plain:
        raise CryptoError(f"明文超过上限: {len(plaintext)} > {max_plain}")
    key = _derive_subkey(data_key, nonce, info)
    ct = AESGCM(key).encrypt(nonce, plaintext, header)
    return header + ct


def decrypt_blob(
    data_key: bytes,
    blob: bytes,
    *,
    magic: bytes,
    info: bytes,
    max_plain: int = config.MANIFEST_MAX_PLAINTEXT,
) -> bytes:
    if len(blob) < 24 + config.GCM_TAG_SIZE:
        raise CryptoError("blob 过短 (被截断?)")
    header = blob[:24]
    if header[:8] != magic:
        raise CryptoError("blob magic 不符")
    nonce = header[8:20]
    plen = struct.unpack(">I", header[20:24])[0]
    if plen > max_plain:
        raise CryptoError(f"声明明文超上限: {plen} > {max_plain}")
    key = _derive_subkey(data_key, nonce, info)
    try:
        return AESGCM(key).decrypt(nonce, blob[24:], header)
    except InvalidTag as exc:
        raise CryptoError("blob 校验失败: 密钥不符 (盘不对) 或数据被篡改") from exc
