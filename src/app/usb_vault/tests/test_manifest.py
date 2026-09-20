"""manifest 单测: JSON 往返 / 字段校验 / 加解密 / 超限。"""

from __future__ import annotations

import json
import os

import pytest

from src.app.usb_vault import config
from src.app.usb_vault.crypto import CryptoError, derive_data_key
from src.app.usb_vault.manifest import (
    FileEntry,
    Manifest,
    ManifestError,
    decrypt_manifest,
    encrypt_manifest,
    manifest_from_json,
    manifest_to_json,
    now_iso,
)


def make_manifest(**overrides: object) -> Manifest:
    base = dict(
        version=1,
        title="项目资料",
        created_at=now_iso(),
        expire="2026-12-31",
        max_opens=None,
        files=[
            FileEntry(id="a" * 32, rel_path="合同/报价单.docx", size=100, enc_size=150,
                      sha256="b" * 64, enc_sha256="c" * 64, media="pdf",
                      converted_from="docx"),
            FileEntry(id="d" * 32, rel_path="说明.txt", size=10, enc_size=60,
                      sha256="e" * 64, enc_sha256="f" * 64, media="text"),
        ],
    )
    base.update(overrides)
    return Manifest(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------ JSON

def test_json_roundtrip() -> None:
    m = make_manifest()
    out = manifest_from_json(manifest_to_json(m))
    assert out.title == m.title
    assert out.expire == m.expire
    assert out.files[0].converted_from == "docx"
    assert out.files[1].converted_from is None
    assert [f.rel_path for f in out.files] == [f.rel_path for f in m.files]


def test_json_chinese_readable() -> None:
    text = manifest_to_json(make_manifest())
    assert "项目资料" in text  # ensure_ascii=False


def test_av_scanned_roundtrip() -> None:
    m = make_manifest(av_scanned={"engine": "Windows Defender", "time": now_iso()})
    out = manifest_from_json(manifest_to_json(m))
    assert out.av_scanned == m.av_scanned


def test_av_scanned_absent_is_none() -> None:
    """旧清单 (v1.1 前) 没有 av_scanned 字段 => None, 不报错。"""
    m = make_manifest()
    text = manifest_to_json(m)
    assert json.loads(text)["av_scanned"] is None  # 字段恒写入, None 占位
    data = json.loads(text)
    del data["av_scanned"]
    out = manifest_from_json(json.dumps(data, ensure_ascii=False))
    assert out.av_scanned is None


def test_av_scanned_bad_type_rejected() -> None:
    m = make_manifest()
    data = json.loads(manifest_to_json(m))
    data["av_scanned"] = ["not", "a", "dict"]
    with pytest.raises(ManifestError, match="av_scanned"):
        manifest_from_json(json.dumps(data, ensure_ascii=False))


def test_missing_field_raises() -> None:
    data = json.loads(manifest_to_json(make_manifest()))
    del data["title"]
    with pytest.raises(ManifestError):
        manifest_from_json(json.dumps(data))


def test_bad_expire_raises() -> None:
    data = json.loads(manifest_to_json(make_manifest()))
    data["expire"] = "2026/12/31"  # 错误格式
    with pytest.raises(ManifestError):
        manifest_from_json(json.dumps(data))


def test_bad_media_raises() -> None:
    data = json.loads(manifest_to_json(make_manifest()))
    data["files"][0]["media"] = "video"
    with pytest.raises(ManifestError):
        manifest_from_json(json.dumps(data))


def test_none_fields_roundtrip() -> None:
    m = make_manifest(expire=None, max_opens=5)
    out = manifest_from_json(manifest_to_json(m))
    assert out.expire is None
    assert out.max_opens == 5


def test_find() -> None:
    m = make_manifest()
    assert m.find("说明.txt") is not None
    assert m.find("不存在") is None


# ------------------------------------------------------------------ 加解密

@pytest.fixture(scope="module")
def data_key() -> bytes:
    return derive_data_key(os.urandom(config.SEED_SIZE), os.urandom(16), 0x11223344)


def test_encrypt_roundtrip(data_key: bytes) -> None:
    m = make_manifest()
    out = decrypt_manifest(encrypt_manifest(m, data_key), data_key)
    assert out.title == m.title
    assert out.files == m.files


def test_encrypt_wrong_key_fails(data_key: bytes) -> None:
    blob = encrypt_manifest(make_manifest(), data_key)
    other = derive_data_key(os.urandom(config.SEED_SIZE), os.urandom(16), 0x11223344)
    with pytest.raises(CryptoError):
        decrypt_manifest(blob, other)


def test_tamper_fails(data_key: bytes) -> None:
    blob = bytearray(encrypt_manifest(make_manifest(), data_key))
    blob[-1] ^= 0xFF
    with pytest.raises(CryptoError):
        decrypt_manifest(bytes(blob), data_key)


def test_oversize_manifest_rejected(data_key: bytes) -> None:
    big = make_manifest(
        files=[FileEntry(id=f"{i:032x}", rel_path=f"f{i}.txt", size=1, enc_size=1,
                         sha256="0" * 64, enc_sha256="0" * 64, media="text")
               for i in range(20000)])  # ~5MB JSON
    with pytest.raises(CryptoError):
        encrypt_manifest(big, data_key)
