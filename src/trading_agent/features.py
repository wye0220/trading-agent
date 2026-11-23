"""通用因果特征。纯函数：输入是决策时点已可见的 K 线，输出特征值。不做 I/O。

EMA 和 ATR 用定点整数运算，单位是 1e-9 tick（FIXED_SCALE），每一步向下取整。这样结果确定、
可重现，而且在几千根 K 线上递推也不会让分数的分母无限增长。它们不会被取整到整数 tick。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Mapping, Sequence

from .strategies.base import FIXED_SCALE, Adr, Bar, ClosedTrade, FeatureContext, PriceRange, Swing

EMA_PERIOD = 20
ATR_PERIOD = 14
ROLLING_RANGE_BARS = 20
ADR_LOOKBACK_DAYS = 10
ADR_MIN_VALID_DAYS = 3
HTF_BARS = 10


def ema_series(closes: Sequence[int], period: int = EMA_PERIOD) -> list[int | None]:
    """收盘价的 EMA（定点）。以第一根收盘为种子，α = 2/(period+1)。满 period 根之前为 None。"""
    out: list[int | None] = []
    ema = 0
    for i, c in enumerate(closes):
        x = c * FIXED_SCALE
        ema = x if i == 0 else ema + (2 * (x - ema)) // (period + 1)
        out.append(ema if i + 1 >= period else None)
    return out


def true_ranges(bars: Sequence[Bar]) -> list[int]:
    """真实波幅（整数 tick）。第一根没有前收，用它自己的全幅。"""
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr_series(bars: Sequence[Bar], period: int = ATR_PERIOD) -> list[int | None]:
    """Wilder ATR（定点）。前 period 个真实波幅的简单平均为首值，之后 ATR = (前值×(period-1) + TR)/period。"""
    out: list[int | None] = []
    atr = 0
    acc = 0
    for i, tr in enumerate(true_ranges(bars)):
        if i < period:
            acc += tr
            atr = (acc * FIXED_SCALE) // period if i == period - 1 else 0
            out.append(atr if i == period - 1 else None)
        else:
            atr = (atr * (period - 1) + tr * FIXED_SCALE) // period
            out.append(atr)
    return out


def ema_side_run(closes: Sequence[int], emas: Sequence[int | None]) -> int:
    """截至最后一根，连续收在 EMA 同侧的根数。正数在上方，负数在下方。EMA 不可用或收在线上时中断。"""
    run = 0
    for c, e in zip(reversed(closes), reversed(emas)):
        if e is None:
            break
        side = (c * FIXED_SCALE > e) - (c * FIXED_SCALE < e)
        if side == 0 or (run and (side > 0) != (run > 0)):
            break
        run += side
    return run


def swings(bars: Sequence[Bar], left: int = 2, right: int = 2) -> list[Swing]:
    """左右各 left/right 根严格确认的 swing。等高等低不确认；右侧第 right 根可见后才可用。"""
    out = []
    for i in range(left, len(bars) - right):
        b = bars[i]
        around = [bars[j] for j in range(i - left, i + right + 1) if j != i]
        known = bars[i + right].start_utc
        if all(b.high > o.high for o in around):
            out.append(Swing("HIGH", b.high, b.start_utc, known))
        if all(b.low < o.low for o in around):
            out.append(Swing("LOW", b.low, b.start_utc, known))
    return out


def rolling_range(bars: Sequence[Bar], n: int = ROLLING_RANGE_BARS) -> PriceRange | None:
    """最近 n 根已完成 K 线（含当前根）的高低区间。不足 n 根为 None。"""
    if len(bars) < n:
        return None
    w = bars[-n:]
    return PriceRange(max(b.high for b in w), min(b.low for b in w))


def intraday_rth_range(bars: Sequence[Bar], rth_open_utc: int, rth_close_utc: int) -> PriceRange | None:
    """当日 RTH 截至当前根的高低区间。当日 RTH 还没有 K 线时为 None。"""
    w = [b for b in bars if rth_open_utc <= b.start_utc < rth_close_utc]
    return PriceRange(max(b.high for b in w), min(b.low for b in w)) if w else None


def adr(day_ranges: Sequence[Mapping[str, Any]], lookback: int = ADR_LOOKBACK_DAYS,
        min_valid: int = ADR_MIN_VALID_DAYS) -> Adr | None:
    """ADR：当前交易日之前 lookback 个 RTH 交易日里完整有效日的高低差均值。

    day_ranges 是 context.rth_day_ranges_asof 的结果（不含当前交易日），每项有 complete 和 range_ticks。
    有效日少于 min_valid 个时为 None。
    """
    valid = [d for d in list(day_ranges)[:lookback] if d.get("complete")]
    if len(valid) < min_valid:
        return None
    return Adr(Fraction(sum(int(d["range_ticks"]) for d in valid), len(valid)), len(valid),
               tuple(sorted(str(d["trading_day"]) for d in valid)))


def to_bar(row: Any) -> Bar:
    """阶段 1 的 K 线行（sqlite3.Row 或字典）→ Bar。"""
    g = (lambda k: row[k])
    return Bar(g("bar_start_utc"), g("bar_end_utc"), g("open"), g("high"), g("low"), g("close"), g("volume"),
               g("session_type"), g("trading_day"))


def build_context(*, scope: str, instrument_root: str, contract_id: str, as_of_utc: int, bars: Sequence[Bar],
                  rth_open_utc: int, rth_close_utc: int, day_ranges: Sequence[Mapping[str, Any]] = (),
                  hourly: Sequence[Bar] = (), reference_levels: Mapping[str, Any] | None = None,
                  closed_trades: Sequence[ClosedTrade] = (), known_at_utc: int | None = None,
                  window: int = 64) -> FeatureContext:
    """由当前实际合约的可见 K 线（含 ETH，按时间升序，最后一根是刚收线的当前 K）组装特征上下文。

    调用方保证 bars 只含同一实际合约、在 as_of_utc 已可见的 K 线；换月后只传新合约的 K 线，
    所以指标不会跨合约接续。上下文里只保留最近 window 根 K 线，指标用全部传入的 K 线计算。
    """
    bars = tuple(bars)
    closes = [b.close for b in bars]
    emas = ema_series(closes)
    atrs = atr_series(bars)
    return FeatureContext(
        scope=scope, instrument_root=instrument_root, contract_id=contract_id, as_of_utc=as_of_utc,
        bars=bars[-window:], rth_open_utc=rth_open_utc, rth_close_utc=rth_close_utc,
        ema20=emas[-1], atr14=atrs[-1], ema_side_run=ema_side_run(closes, emas),
        swings=tuple(swings(bars[-window:])), rolling_20_range=rolling_range(bars),
        intraday_rth_range=intraday_rth_range(bars, rth_open_utc, rth_close_utc),
        adr10=adr(day_ranges), htf_1h=tuple(hourly)[-HTF_BARS:],
        reference_levels=reference_levels or {}, closed_trades=tuple(closed_trades), known_at_utc=known_at_utc)
