"""K 线内容摘要：逐根 sha256 当作 256 位整数相加取模。

与顺序无关，所以可以在流式导入时累计，也可以从库里重算，两条路径得到相同的结果。
用于登记留在库外的数据（不保存价格也能核对内容）和数据切分的日期清单。
"""
from __future__ import annotations

import hashlib

_MOD = 1 << 256
EMPTY = "0" * 64


def bar_digest(source: str, contract_id: str, timeframe: str, bar_start_utc: int,
               o: int, h: int, l: int, c: int, v: int) -> int:  # noqa: E741
    raw = f"{source}|{contract_id}|{timeframe}|{bar_start_utc}|{o}|{h}|{l}|{c}|{v}"
    return int.from_bytes(hashlib.sha256(raw.encode()).digest(), "big")


def add(acc: int, digest: int) -> int:
    return (acc + digest) % _MOD


def to_hex(acc: int) -> str:
    return f"{acc:064x}"


def from_hex(text: str | None) -> int:
    return int(text, 16) if text else 0


def merge_hex(a: str | None, b: str | None) -> str:
    return to_hex((from_hex(a) + from_hex(b)) % _MOD)


def of_text(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
