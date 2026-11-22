"""统一 K 线记录与价格换算。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "1h": 3600}
DBN_PRICE_SCALE = 1_000_000_000


class OffTickPrice(ValueError):
    pass


def fixed_to_ticks(raw: int, tick_size: Decimal) -> int:
    """Databento 定点价格（单位 1e-9）→ 整数 tick。不能整除则抛 OffTickPrice。"""
    div = int(tick_size * DBN_PRICE_SCALE)
    q, r = divmod(raw, div)
    if r:
        raise OffTickPrice(f"{raw / DBN_PRICE_SCALE} 不是 {tick_size} 的整数倍")
    return q


def decimal_to_ticks(value, tick_size: Decimal) -> int:
    """十进制价格（字符串或数字）→ 整数 tick。"""
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise OffTickPrice(f"无法解析价格：{value!r}") from None
    q = d / tick_size
    if q != q.to_integral_value():
        raise OffTickPrice(f"{d} 不是 {tick_size} 的整数倍")
    return int(q)


def ticks_to_price(ticks: int, tick_size: Decimal) -> Decimal:
    return ticks * tick_size


@dataclass(slots=True)
class RawBar:
    """适配器输出的一根原始 K 线（价格已是整数 tick）。"""
    source: str
    instrument_root: str
    contract_id: str
    timeframe: str
    bar_start_utc: int
    open: int
    high: int
    low: int
    close: int
    volume: int
    fetched_at_utc: int | None = None     # 来源给的取数时刻；没有则为 None，入库时估计
    fixture: bool = False

    @property
    def bar_end_utc(self) -> int:
        return self.bar_start_utc + TIMEFRAME_SECONDS[self.timeframe]

    def content(self) -> tuple:
        return (self.open, self.high, self.low, self.close, self.volume)
