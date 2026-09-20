"""unlock_code 单测: 生成/规范化/展示/派生分量/归档行。"""

from __future__ import annotations

import json
import re

import pytest

from src.app.usb_vault import unlock_code

_VALID = "ABCDEFGH"  # 8 个合法字符
_SHAPE = re.compile(r"^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{8}$")


def test_generate_shape() -> None:
    for _ in range(30):
        assert _SHAPE.match(unlock_code.generate())


def test_generate_unique() -> None:
    codes = {unlock_code.generate() for _ in range(300)}
    assert len(codes) == 300  # 39bit 空间, 300 样本碰撞概率可忽略


def test_alphabet_excludes_confusable() -> None:
    for ch in "01ILOU":
        assert ch not in unlock_code.ALPHABET


def test_normalize_strips_separators_and_uppercases() -> None:
    assert unlock_code.normalize("abcd-efgh") == _VALID


def test_normalize_rejects_bad_length() -> None:
    with pytest.raises(unlock_code.UnlockCodeError, match="8 位"):
        unlock_code.normalize("ABCD-EFG")  # 7 位


def test_normalize_rejects_bad_chars() -> None:
    with pytest.raises(unlock_code.UnlockCodeError, match="非法字符"):
        unlock_code.normalize("ABCD-EFG0")  # 0 不在字母表


def test_display_groups_by_four() -> None:
    assert unlock_code.display(_VALID) == "ABCD-EFGH"


def test_derive_component_is_ascii_of_normalized() -> None:
    assert unlock_code.derive_component("abcd efgh") == _VALID.encode("ascii")


def test_archive_line_json_fields() -> None:
    line = unlock_code.archive_line(_VALID, title="项目资料", serial=0x11223344)
    rec = json.loads(line)
    assert rec["code"] == _VALID
    assert rec["title"] == "项目资料"
    assert rec["serial"] == "0x11223344"  # #010x: 0x + 8 位十六进制
    assert "T" in rec["created"]  # ISO 时间戳


def test_archive_line_rejects_invalid_code() -> None:
    with pytest.raises(unlock_code.UnlockCodeError):
        unlock_code.archive_line("short", title="t", serial=1)
