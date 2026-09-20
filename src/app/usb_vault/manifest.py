"""清单数据模型与加解密 (manifest.enc)。

清单是盘上唯一的"结构信息": 文件树、标题、有效期、打开次数上限。
文件名不落明文 —— 全部包在这份加密清单里。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.app.usb_vault import config
from src.app.usb_vault.crypto import decrypt_blob, encrypt_blob


class ManifestError(Exception):
    """清单序列化/反序列化失败。"""


@dataclass(slots=True)
class FileEntry:
    """单个已烧录文件。rel_path 用 "/" 分隔 (展示与树构建用)。"""

    id: str  # 32 hex 随机 id, DATA/<id>.enc
    rel_path: str
    size: int  # 明文字节数
    enc_size: int
    sha256: str  # 明文 SHA-256 hex
    enc_sha256: str  # 密文 SHA-256 hex
    media: str  # text / image / pdf / binary
    converted_from: str | None = None  # Office->PDF 时记录原扩展名


@dataclass(slots=True)
class Manifest:
    version: int
    title: str
    created_at: str  # ISO 8601 datetime
    expire: str | None  # ISO date (YYYY-MM-DD) 或 None=永久
    max_opens: int | None
    files: list[FileEntry] = field(default_factory=list)
    # 可选审计字段 (v1.1 起): 烧录前病毒扫描记录 {"engine": ..., "time": ...}
    # 旧清单无此字段 => None; 查看器忽略, 仅 verify/inspect 展示
    av_scanned: dict[str, str] | None = None

    def find(self, rel_path: str) -> FileEntry | None:
        return next((f for f in self.files if f.rel_path == rel_path), None)


# ---------------------------------------------------------------------------
# JSON 往返
# ---------------------------------------------------------------------------

def manifest_to_json(m: Manifest) -> str:
    """序列化为紧凑 JSON (ensure_ascii=False, 中文文件名可读性更好)。"""
    data = {
        "version": m.version,
        "title": m.title,
        "created_at": m.created_at,
        "expire": m.expire,
        "max_opens": m.max_opens,
        "av_scanned": m.av_scanned,
        "files": [
            {
                "id": f.id,
                "rel_path": f.rel_path,
                "size": f.size,
                "enc_size": f.enc_size,
                "sha256": f.sha256,
                "enc_sha256": f.enc_sha256,
                "media": f.media,
                "converted_from": f.converted_from,
            }
            for f in m.files
        ],
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _require(d: dict[str, object], key: str) -> Any:
    if key not in d:
        raise ManifestError(f"清单缺少字段: {key}")
    return d[key]


def manifest_from_json(text: str) -> Manifest:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"清单 JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("清单顶层必须是对象")

    expire_raw = _require(data, "expire")
    if expire_raw is not None:
        try:
            date.fromisoformat(str(expire_raw))  # 仅校验格式
        except ValueError as exc:
            raise ManifestError(f"expire 日期格式错误: {expire_raw}") from exc

    files_raw = _require(data, "files")
    if not isinstance(files_raw, list):
        raise ManifestError("files 必须是列表")
    files: list[FileEntry] = []
    for i, item in enumerate(files_raw):
        if not isinstance(item, dict):
            raise ManifestError(f"files[{i}] 必须是对象")
        media = str(_require(item, "media"))
        if media not in config.MEDIA_TYPES:
            raise ManifestError(f"files[{i}].media 非法: {media}")
        files.append(
            FileEntry(
                id=str(_require(item, "id")),
                rel_path=str(_require(item, "rel_path")),
                size=int(_require(item, "size")),
                enc_size=int(_require(item, "enc_size")),
                sha256=str(_require(item, "sha256")),
                enc_sha256=str(_require(item, "enc_sha256")),
                media=media,
                converted_from=(str(item["converted_from"])
                                if item.get("converted_from") is not None else None),
            )
        )
    av_raw = data.get("av_scanned")
    if av_raw is not None and not isinstance(av_raw, dict):
        raise ManifestError("av_scanned 必须是对象")
    return Manifest(
        version=int(_require(data, "version")),
        title=str(_require(data, "title")),
        created_at=str(_require(data, "created_at")),
        expire=(None if expire_raw is None else str(expire_raw)),
        max_opens=(None if _require(data, "max_opens") is None
                   else int(_require(data, "max_opens"))),
        files=files,
        av_scanned=({str(k): str(v) for k, v in av_raw.items()}
                    if av_raw is not None else None),
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 加解密
# ---------------------------------------------------------------------------

def encrypt_manifest(m: Manifest, data_key: bytes) -> bytes:
    plain = manifest_to_json(m).encode("utf-8")
    return encrypt_blob(
        data_key, plain, magic=config.MAGIC_MANIFEST, info=config.HKDF_INFO_MANIFEST
    )


def decrypt_manifest(blob: bytes, data_key: bytes) -> Manifest:
    plain = decrypt_blob(
        data_key, blob, magic=config.MAGIC_MANIFEST, info=config.HKDF_INFO_MANIFEST
    )
    return manifest_from_json(plain.decode("utf-8"))
