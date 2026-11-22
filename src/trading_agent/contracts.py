"""实际合约：代码规范化、到期日、换月规则。"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .config import ConfigError, Instrument

MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
_RAW = re.compile(r"^([A-Z0-9]+?)([FGHJKMNQUVXZ])(\d{1,4})$")


class ContractError(ValueError):
    pass


def is_spread(raw_symbol: str) -> bool:
    return "-" in raw_symbol or ":" in raw_symbol or " " in raw_symbol


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    return d + timedelta(days=14)


def parse(contract_id: str) -> tuple[str, str, int]:
    m = _RAW.match(contract_id)
    if not m or len(m.group(3)) != 4:
        raise ContractError(f"不是规范的 contract_id：{contract_id!r}")
    return m.group(1), m.group(2), int(m.group(3))


def expiry(contract_id: str) -> date:
    root, code, year = parse(contract_id)
    return third_friday(year, MONTH_CODES[code])


def normalize(raw_symbol: str, ref_date: date) -> str:
    """来源短代码 → 四位年份的 contract_id。

    一位年份：取不早于 ref_date 的、个位数一致且尚未到期的第一个年份。
    ref_date 用符号映射区间的起始日期（或 K 线所在日期）。
    """
    if is_spread(raw_symbol):
        raise ContractError(f"价差合约不是单腿：{raw_symbol!r}")
    m = _RAW.match(raw_symbol)
    if not m:
        raise ContractError(f"无法解析合约代码：{raw_symbol!r}")
    root, code, yr = m.group(1), m.group(2), m.group(3)
    month = MONTH_CODES[code]
    if len(yr) == 4:
        return f"{root}{code}{yr}"
    if len(yr) == 3:
        raise ContractError(f"无法解析合约年份：{raw_symbol!r}")
    span = 10 if len(yr) == 1 else 100
    digit = int(yr)
    y = ref_date.year
    for _ in range(span + 1):
        if y % span == digit and third_friday(y, month) >= ref_date:
            return f"{root}{code}{y}"
        y += 1
    raise ContractError(f"无法解析合约年份：{raw_symbol!r}")


def short_symbol(contract_id: str) -> str:
    """contract_id → 一位年份的交易所短代码（如 ESZ6），供向行情源请求。"""
    root, code, year = parse(contract_id)
    return f"{root}{code}{year % 10}"


def roll_date(contract_id: str, rule: dict) -> date:
    """该合约停止作为实际合约的第一个交易日（含）。"""
    if rule.get("kind") != "days_before_expiry" or rule.get("expiry") != "third_friday":
        raise ConfigError(f"不支持的换月规则：{rule!r}")
    return expiry(contract_id) - timedelta(days=int(rule["days_before_expiry"]))


def contract_chain(inst: Instrument, start_year: int, end_year: int) -> list[str]:
    order = sorted(inst.months, key=lambda c: MONTH_CODES[c])
    return [f"{inst.root}{c}{y}" for y in range(start_year, end_year + 1) for c in order]


def active_contract(inst: Instrument, trading_day: date, rules: dict[str, dict]) -> str:
    """换月规则下，该交易日的实际合约。"""
    rule = rules[inst.roll_rule_version]
    for cid in contract_chain(inst, trading_day.year - 1, trading_day.year + 1):
        if trading_day < roll_date(cid, rule):
            return cid
    raise ContractError(f"找不到 {inst.root} 在 {trading_day} 的实际合约")


def roll_schedule(inst: Instrument, start: date, end: date, rules: dict[str, dict]) -> list[tuple[date, str, str]]:
    """区间内的换月：[(换月日, 旧合约, 新合约)]。"""
    rule = rules[inst.roll_rule_version]
    chain = contract_chain(inst, start.year - 1, end.year + 1)
    out = []
    for old, new in zip(chain, chain[1:]):
        rd = roll_date(old, rule)
        if start <= rd <= end:
            out.append((rd, old, new))
    return out
