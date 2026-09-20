"""每盘解锁码 (合同⑤: 每套U盘独立唯一解锁密码)。

- 生成: 8 字符随机码, 字母表 30 字符 (去掉 0/1/I/L/O/U —— 手抄/口述
  不易混淆, 也避免拼出敏感词), 约 39.3 bit 熵 (30^8 ≈ 6.6e11);
  穷举防护主要靠 scrypt 内存硬度 (每次尝试 ~32MB/数十毫秒), 在线侧
  另有查看器 3 次错误熔断 —— 码渠道保密是安全前提 (2026-09 由 16 位改 8 位)
- 规范化: 用户输入允许小写、连字符/空格分组 (XXXX-XXXX);
  统一为大写去分隔符后校验字符集与长度
- 用途: data_key = scrypt(码‖SEED, nonce‖序列号) —— 码与 SEED 缺一不可,
  泄露任意单者都不足以解密

归档义务 (合同): 烧录端写入 secrets/unlock_codes.jsonl (gitignore),
留存期限不超过质保期届满, 由乙方流程保证销毁。
"""

from __future__ import annotations

import json
import secrets as pysecrets
from datetime import datetime

ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # 30 字符, 无 0/1/I/L/O/U
CODE_LEN = 8
DISPLAY_GROUP = 4  # XXXX-XXXX


class UnlockCodeError(Exception):
    """解锁码格式非法 (长度/字符集不符)。"""


def generate() -> str:
    """生成 8 位随机解锁码 (密码学随机源)。"""
    return "".join(pysecrets.choice(ALPHABET) for _ in range(CODE_LEN))


def normalize(code: str) -> str:
    """统一输入格式: 去分隔符/空白 -> 大写; 校验字符集与长度。

    非法输入抛 UnlockCodeError (调用方按"码错误"处理, 不放行)。
    """
    s = code.replace("-", "").replace(" ", "").strip().upper()
    if len(s) != CODE_LEN:
        raise UnlockCodeError(f"解锁码应为 {CODE_LEN} 位, 收到 {len(s)} 位")
    bad = set(s) - set(ALPHABET)
    if bad:
        raise UnlockCodeError(f"解锁码含非法字符: {''.join(sorted(bad))}")
    return s


def display(code: str) -> str:
    """展示格式 XXXX-XXXX-XXXX-XXXX (打印密封交甲方用)。"""
    s = normalize(code)
    return "-".join(s[i:i + DISPLAY_GROUP] for i in range(0, CODE_LEN, DISPLAY_GROUP))


def derive_component(code: str) -> bytes:
    """码 -> scrypt 口令分量 (normalize 后转 ASCII 字节)。"""
    return normalize(code).encode("ascii")


def archive_line(code: str, *, title: str, serial: int) -> str:
    """归档 JSONL 行 (一行一盘: 时间/标题/序列号/码)。"""
    s = normalize(code)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    return json.dumps({"created": ts, "title": title, "serial": f"{serial:#010x}",
                       "code": s}, ensure_ascii=False)
