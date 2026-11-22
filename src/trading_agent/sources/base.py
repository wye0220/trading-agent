"""适配器共用的类型。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Anomaly:
    category: str
    source: str | None = None
    instrument_root: str | None = None
    contract_id: str | None = None
    timeframe: str | None = None
    start_utc: int | None = None
    end_utc: int | None = None
    trading_day: str | None = None
    detail: str = ""


class UnsupportedSchema(Exception):
    pass


class RejectedFile(Exception):
    pass


def is_fixture_name(name: str) -> bool:
    return "fixture" in name.lower()
