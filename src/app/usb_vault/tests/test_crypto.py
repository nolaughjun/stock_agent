"""crypto 单测: 往返/篡改检测/流式一致性 + seed 读写。

篡改矩阵: magic / nonce / nchunks / plen / 密文字节 / tag /
截断末块 / 末尾续接 / 块交换 —— 全部必须在解密侧抛 CryptoError。
"""

from __future__ import annotations

import io
import os
import struct
from pathlib import Path

import pytest

from src.app.usb_vault import config, seed
from src.app.usb_vault.crypto import (
    CryptoError,
    decrypt_blob,
    decrypt_chunks,
    derive_data_key,
    encrypt_blob,
    encrypt_stream,
)

CHUNK = 64  # 小块参数化, 触发多块路径

SEED = os.urandom(config.SEED_SIZE)
NONCE = os.urandom(16)
SERIAL = 0x11223344


@pytest.fixture(scope="module")
def data_key() -> bytes:
    return derive_data_key(SEED, NONCE, SERIAL)


def _encrypt(key: bytes, plain: bytes) -> bytes:
    out = io.BytesIO()
    encrypt_stream(key, io.BytesIO(plain), out, plain_len=len(plain), chunk_size=CHUNK)
    return out.getvalue()


def _decrypt(key: bytes, blob: bytes) -> bytes:
    return b"".join(decrypt_chunks(key, io.BytesIO(blob), chunk_size=CHUNK))


# ------------------------------------------------------------------ 往返

@pytest.mark.parametrize("size", [0, 1, 63, 64, 65, 200, 1000])
def test_roundtrip(data_key: bytes, size: int) -> None:
    plain = os.urandom(size)
    assert _decrypt(data_key, _encrypt(data_key, plain)) == plain


def test_roundtrip_wrong_key_fails(data_key: bytes) -> None:
    blob = _encrypt(data_key, b"secret" * 20)
    other = derive_data_key(os.urandom(config.SEED_SIZE), NONCE, SERIAL)
    with pytest.raises(CryptoError):
        _decrypt(other, blob)


def test_streaming_equals_whole(data_key: bytes) -> None:
    plain = os.urandom(CHUNK * 3 + 7)
    blob = _encrypt(data_key, plain)
    parts = list(decrypt_chunks(data_key, io.BytesIO(blob), chunk_size=CHUNK))
    assert len(parts) == 4  # 3 满块 + 1 尾块
    assert b"".join(parts) == plain


def test_empty_file_single_chunk(data_key: bytes) -> None:
    blob = _encrypt(data_key, b"")
    nchunks = struct.unpack(">I", blob[20:24])[0]
    assert nchunks == 1
    assert _decrypt(data_key, blob) == b""


def test_encrypt_length_mismatch(data_key: bytes) -> None:
    with pytest.raises(CryptoError):
        encrypt_stream(data_key, io.BytesIO(b"short"), io.BytesIO(),
                       plain_len=100, chunk_size=CHUNK)


# ------------------------------------------------------------------ 篡改

def _blocks(blob: bytes) -> list[tuple[int, int]]:
    """解析 .enc 布局, 返回每块 (密文偏移, 密文长度)。"""
    nchunks = struct.unpack(">I", blob[20:24])[0]
    plen = struct.unpack(">Q", blob[24:32])[0]
    last = plen - CHUNK * (nchunks - 1)
    out = []
    off = 32
    for i in range(nchunks):
        n = last if i == nchunks - 1 else CHUNK
        out.append((off, n))
        off += n + config.GCM_TAG_SIZE
    return out


def _flip(data: bytearray, pos: int) -> bytearray:
    data[pos] ^= 0xFF
    return data


@pytest.mark.parametrize("field,offset", [
    ("magic", 0), ("nonce", 8), ("nchunks", 20), ("plen", 24),
])
def test_header_tamper(data_key: bytes, field: str, offset: int) -> None:
    blob = _encrypt(data_key, b"x" * (CHUNK * 2 + 5))
    with pytest.raises(CryptoError):
        _decrypt(data_key, bytes(_flip(bytearray(blob), offset)))


def test_ciphertext_tamper(data_key: bytes) -> None:
    blob = _encrypt(data_key, os.urandom(100))
    off, _n = _blocks(blob)[1]
    with pytest.raises(CryptoError):
        _decrypt(data_key, bytes(_flip(bytearray(blob), off + 3)))


def test_tag_tamper(data_key: bytes) -> None:
    blob = _encrypt(data_key, os.urandom(100))
    off, n = _blocks(blob)[0]
    with pytest.raises(CryptoError):
        _decrypt(data_key, bytes(_flip(bytearray(blob), off + n + 2)))


def test_truncated_tail(data_key: bytes) -> None:
    blob = _encrypt(data_key, os.urandom(CHUNK * 2))
    with pytest.raises(CryptoError):
        _decrypt(data_key, blob[:-5])


def test_appended_tail(data_key: bytes) -> None:
    blob = _encrypt(data_key, b"abc")
    with pytest.raises(CryptoError):
        _decrypt(data_key, blob + b"\x00")


def test_swapped_blocks(data_key: bytes) -> None:
    plain = b"A" * CHUNK + b"B" * CHUNK + b"C" * 10
    blob = bytearray(_encrypt(data_key, plain))
    (a_off, a_n), (b_off, b_n) = _blocks(bytes(blob))[:2]
    block_a = bytes(blob[a_off:a_off + a_n + config.GCM_TAG_SIZE])
    block_b = bytes(blob[b_off:b_off + b_n + config.GCM_TAG_SIZE])
    assert a_n == b_n == CHUNK
    blob[a_off:a_off + a_n + config.GCM_TAG_SIZE] = block_b
    blob[b_off:b_off + b_n + config.GCM_TAG_SIZE] = block_a
    with pytest.raises(CryptoError):
        _decrypt(data_key, bytes(blob))


# ------------------------------------------------------------------ blob (manifest)

def test_blob_roundtrip(data_key: bytes) -> None:
    blob = encrypt_blob(data_key, "清单内容".encode(), magic=config.MAGIC_MANIFEST,
                        info=config.HKDF_INFO_MANIFEST)
    out = decrypt_blob(data_key, blob, magic=config.MAGIC_MANIFEST,
                       info=config.HKDF_INFO_MANIFEST)
    assert out.decode() == "清单内容"


def test_blob_wrong_magic(data_key: bytes) -> None:
    blob = encrypt_blob(data_key, b"x", magic=b"AAAA\x00\x00\x00\x00",
                        info=config.HKDF_INFO_MANIFEST)
    with pytest.raises(CryptoError):
        decrypt_blob(data_key, blob, magic=config.MAGIC_MANIFEST,
                     info=config.HKDF_INFO_MANIFEST)


def test_blob_too_short(data_key: bytes) -> None:
    with pytest.raises(CryptoError):
        decrypt_blob(data_key, b"short", magic=config.MAGIC_MANIFEST,
                     info=config.HKDF_INFO_MANIFEST)


# ------------------------------------------------------------------ 密钥派生

def test_derive_key_deterministic_and_serial_bound() -> None:
    k1 = derive_data_key(SEED, NONCE, SERIAL)
    k2 = derive_data_key(SEED, NONCE, SERIAL)
    k3 = derive_data_key(SEED, NONCE, SERIAL + 1)  # 换盘 => 换钥
    k4 = derive_data_key(SEED, os.urandom(16), SERIAL)  # 换盐 => 换钥
    assert k1 == k2 and len(k1) == 32
    assert k1 != k3 and k1 != k4


def test_derive_key_bad_inputs() -> None:
    with pytest.raises(CryptoError):
        derive_data_key(b"short", NONCE, SERIAL)
    with pytest.raises(CryptoError):
        derive_data_key(SEED, b"short", SERIAL)


def test_derive_key_unlock_code_participates() -> None:
    """密码模式: 码参与口令 => 无码/错码/换码 三者派生出互不相同的密钥。"""
    code = b"ABCDEFGHJKMNPQRS"
    base = derive_data_key(SEED, NONCE, SERIAL)
    with_code = derive_data_key(SEED, NONCE, SERIAL, unlock_code=code)
    wrong_code = derive_data_key(SEED, NONCE, SERIAL, unlock_code=b"BBBBBBBBBBBBBBBB")
    assert with_code != base  # 只拿 SEED 解不开密码盘
    assert with_code != wrong_code  # 错码 => 换钥
    assert derive_data_key(SEED, NONCE, SERIAL, unlock_code=code) == with_code  # 确定


# ------------------------------------------------------------------ seed

def test_seed_create_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "seed.bin"
    data = seed.create_seed(p)
    assert len(data) == config.SEED_SIZE
    assert seed.load_seed(p) == data


def test_seed_exclusive_create(tmp_path: Path) -> None:
    p = tmp_path / "seed.bin"
    seed.create_seed(p)
    with pytest.raises(seed.SeedError):
        seed.create_seed(p)  # 不许覆盖


def test_seed_missing(tmp_path: Path) -> None:
    with pytest.raises(seed.SeedError):
        seed.load_seed(tmp_path / "nope.bin")


def test_seed_wrong_size(tmp_path: Path) -> None:
    p = tmp_path / "seed.bin"
    p.write_bytes(b"short")
    with pytest.raises(seed.SeedError):
        seed.load_seed(p)


def test_seed_never_text_translated(tmp_path: Path) -> None:
    """回归: Windows 上 os.open 缺 O_BINARY 会走文本模式, 0x0A 变 0D 0A。

    64B 随机数单次含 0x0A 概率约 22%, 30 轮全干净的概率 <0.1%,
    若翻译复活此测试必然抓住 (create_seed 自带回读校验会直接抛 SeedError)。
    """
    for i in range(30):
        data = seed.create_seed(tmp_path / f"seed_{i:02d}.bin")
        assert len(data) == config.SEED_SIZE
