from fractions import Fraction

import helpers as H
from trading_agent import features as F
from trading_agent.strategies.base import FIXED_SCALE as S, Bar, ClosedTrade, PriceRange

RTH_O, RTH_C = 10_000, 10_000 + 78 * 300


def mk(i, o, h, l, c, start=None, sess="RTH"):
    st = 1000 + 300 * i if start is None else start
    return Bar(st, st + 300, o, h, l, c, 1, sess, "2025-11-25")


def flat(n, px=100):
    return [mk(i, px, px + 2, px - 2, px) for i in range(n)]


# ---------------------------------------------------------------- 3.1 EMA
def test_ema_warmup_and_constant():
    assert F.ema_series([100] * 19)[-1] is None
    assert F.ema_series([100] * 20)[-1] == 100 * S
    out = F.ema_series([100] * 25)
    assert out[:19] == [None] * 19 and out[19:] == [100 * S] * 6


def test_ema_matches_hand_calculation():
    closes = list(range(100, 125))
    ema = Fraction(closes[0])
    for c in closes[1:]:
        ema += Fraction(2, 21) * (c - ema)
    got = F.ema_series(closes)[-1]
    assert abs(Fraction(got, S) - ema) < Fraction(1, 10**6)       # 定点误差远小于 1e-6 tick
    assert got % S != 0                                             # 没有被取整到整数 tick


# ---------------------------------------------------------------- 3.2 ATR
def test_true_range_uses_previous_close():
    bars = [mk(0, 100, 104, 98, 100), mk(1, 110, 112, 109, 111), mk(2, 111, 111, 100, 101)]
    assert F.true_ranges(bars) == [6, 12, 11]        # 第 2 根：高 - 前收 = 12；第 3 根：前收 - 低 = 11


def test_atr_first_value_and_recursion():
    bars = [mk(i, 100, 100 + (i % 3) + 2, 100 - 1, 100) for i in range(13)]
    assert F.atr_series(bars)[-1] is None
    bars.append(mk(13, 100, 105, 99, 100))
    trs = F.true_ranges(bars)
    first = F.atr_series(bars)[-1]
    assert len(trs) == 14 and first == (sum(trs) * S) // 14
    bars.append(mk(14, 100, 120, 99, 100))
    nxt = F.atr_series(bars)[-1]
    assert nxt == (first * 13 + 21 * S) // 14
    assert first % S != 0                              # 保留小数部分，不取整到整数 tick
    ref = Fraction(sum(trs), 14)
    assert abs(Fraction(first, S) - ref) < Fraction(1, 10**8)
    assert abs(Fraction(nxt, S) - (ref * 13 + 21) / 14) < Fraction(1, 10**8)


# ---------------------------------------------------------------- 3.3 swing
def swing_bars(highs, lows=None):
    lows = lows or [h - 10 for h in highs]
    return [mk(i, l + 1, h, l, l + 2) for i, (h, l) in enumerate(zip(highs, lows))]


def test_swing_needs_two_bars_on_the_right():
    assert [s for s in F.swings(swing_bars([10, 11, 15, 12])) if s.kind == "HIGH"] == []
    (s,) = [s for s in F.swings(swing_bars([10, 11, 15, 12, 11])) if s.kind == "HIGH"]
    assert s.price == 15 and s.bar_start_utc == 1000 + 600 and s.known_at_bar_start_utc == 1000 + 1200


def test_equal_high_is_not_a_swing():
    assert [s for s in F.swings(swing_bars([10, 11, 15, 15, 11])) if s.kind == "HIGH"] == []
    assert [s for s in F.swings(swing_bars([10, 15, 15, 12, 11])) if s.kind == "HIGH"] == []


def test_swing_low_mirror():
    lows = [20, 19, 15, 18, 19]
    (s,) = [s for s in F.swings(swing_bars([l + 10 for l in lows], lows)) if s.kind == "LOW"]
    assert s.price == 15
    assert [s for s in F.swings(swing_bars([30] * 5, [20, 19, 15, 15, 19])) if s.kind == "LOW"] == []


# ---------------------------------------------------------------- 3.4 区间
def test_rolling_range_needs_20():
    bars = flat(19)
    assert F.rolling_range(bars) is None
    bars.append(mk(19, 100, 110, 90, 100))
    assert F.rolling_range(bars) == PriceRange(110, 90)
    bars += flat(20)                                   # 那根宽 K 滑出窗口
    assert F.rolling_range(bars) == PriceRange(102, 98)


def test_overnight_high_only_in_rolling_range():
    eth = [mk(i, 100, 150 if i == 5 else 102, 98, 100, start=RTH_O - 300 * (12 - i), sess="ETH") for i in range(12)]
    rth = [mk(i, 100, 104, 97, 100, start=RTH_O + 300 * i) for i in range(8)]
    bars = eth + rth
    assert F.rolling_range(bars) == PriceRange(150, 97)
    assert F.intraday_rth_range(bars, RTH_O, RTH_C) == PriceRange(104, 97)
    assert F.intraday_rth_range(eth, RTH_O, RTH_C) is None


# ---------------------------------------------------------------- 3.5 ADR 与 1 小时线
def day(i, complete=True, rng=200):
    d = {"trading_day": f"2025-11-{10 + i:02d}", "complete": complete}
    if complete:
        d["range_ticks"] = rng
    else:
        d["reason"] = "UNEXPECTED_GAP:1"
    return d


def test_adr_needs_three_valid_days():
    assert F.adr([day(0), day(1)] + [day(i, False) for i in range(2, 10)]) is None
    a = F.adr([day(0, rng=100), day(1, rng=200), day(2, rng=301)] + [day(i, False) for i in range(3, 10)])
    assert a.value_ticks == Fraction(601, 3) and a.valid_days == 3


def test_adr_partial_validity_and_lookback():
    rows = [day(i, complete=i not in (1, 4, 8), rng=100 + 10 * i) for i in range(10)]
    a = F.adr(rows)
    valid = [100 + 10 * i for i in range(10) if i not in (1, 4, 8)]
    assert a.valid_days == 7 and a.value_ticks == Fraction(sum(valid), 7) and len(a.days) == 7
    b = F.adr([day(i, rng=100) for i in range(10)] + [day(10, rng=100000)])        # 第 11 天不计入
    assert b.value_ticks == 100 and b.valid_days == 10
    assert F.adr([]) is None


def test_adr_excludes_current_day_with_real_asof_query(env):
    """用阶段 1 的 as-of 查询喂 ADR：当天不在其中，有缺口的交易日不算有效日。"""
    from datetime import date
    from trading_agent import context, sessions
    days = sessions.trading_days(date(2025, 11, 17), date(2025, 11, 25))
    rows = []
    for i, d in enumerate(days):
        rows += H.rth_bars(d, skip=[(11, 0)] if i == 2 else ())
    H.write_csv(env.root / "fixture.csv", rows)
    env.ingest()
    ranges = context.rth_day_ranges_asof(env.con, env.settings, "ES", H.et(days[-1], 12, 0), 10)
    a = F.adr(ranges)
    assert days[-1].isoformat() not in a.days and a.valid_days == len(days) - 2
    assert days[2].isoformat() not in a.days


def test_hourly_context_excludes_unfinished_hour(env):
    from datetime import date
    from trading_agent import context
    d = date(2025, 11, 25)
    H.write_csv(env.root / "fixture.csv", H.eth_bars(d) + H.rth_bars(d))
    env.ingest()
    hourly = [F.to_bar(r) for r in context.hourly_asof(env.con, "fixsrc", "ESZ2025", H.et(d, 10, 40))]
    assert hourly[-1].end_utc == H.et(d, 10, 0) and all(b.end_utc - b.start_utc == 3600 for b in hourly)
    ctx = F.build_context(scope="s", instrument_root="ES", contract_id="ESZ2025", as_of_utc=H.et(d, 10, 40),
                          bars=flat(30), rth_open_utc=0, rth_close_utc=1, hourly=hourly + hourly + hourly)
    assert len(ctx.htf_1h) == 10


# ---------------------------------------------------------------- 3.6 上下文组装
def build(bars, **kw):
    return F.build_context(scope="pa_brooks:ES:RTH:5m", instrument_root="ES", contract_id="ESH2026",
                           as_of_utc=bars[-1].end_utc + 5, bars=bars, rth_open_utc=RTH_O, rth_close_utc=RTH_C, **kw)


def test_context_after_roll_has_no_indicators():
    """换月后调用方只传新合约的 K 线：只有 10 根时 EMA、ATR 都不可用。"""
    ctx = build(flat(10))
    assert ctx.ema20 is None and ctx.atr14 is None and ctx.rolling_20_range is None and ctx.ema_side_run == 0
    ctx = build(flat(20))
    assert ctx.ema20 == 100 * S and ctx.atr14 == 4 * S and ctx.rolling_20_range == PriceRange(102, 98)


def test_context_fields():
    bars = [mk(i, 100 + i, 103 + i, 99 + i, 102 + i, start=RTH_O + 300 * i) for i in range(30)]
    tr = ClosedTrade("x", "STOP", -1)
    ctx = build(bars, day_ranges=[day(i) for i in range(5)], reference_levels={"pdh": 500}, closed_trades=[tr],
                known_at_utc=123, window=8)
    assert len(ctx.bars) == 8 and ctx.bar is bars[-1] and ctx.prev_bar is bars[-2]
    assert ctx.ema_side_run == 11                       # EMA 从第 20 根起可用，上升序列一直收在其上方
    assert ctx.adr10.valid_days == 5 and ctx.reference_levels["pdh"] == 500 and ctx.closed_trades == (tr,)
    assert ctx.intraday_rth_range == PriceRange(132, 99) and ctx.known_at_utc == 123
    assert ctx.swings == ()                             # 单调上升没有 swing


def test_ema_side_run_sign_and_break():
    closes = [100] * 20 + [90] * 3
    emas = F.ema_series(closes)
    assert F.ema_side_run(closes, emas) == -3
    closes2 = closes + [200]
    assert F.ema_side_run(closes2, F.ema_series(closes2)) == 1
    assert F.ema_side_run([100] * 20, F.ema_series([100] * 20)) == 0       # 正好收在线上


def test_features_ignore_future_bars():
    import random
    rnd = random.Random(7)
    bars, px = [], 20000
    for i in range(80):
        o = px
        c = o + rnd.randint(-12, 12)
        bars.append(mk(i, o, max(o, c) + rnd.randint(0, 6), min(o, c) - rnd.randint(0, 6), c, start=RTH_O + 300 * i))
        px = c
    early = build(bars[:50])
    assert F.ema_series([b.close for b in bars])[49] == early.ema20
    assert F.atr_series(bars)[49] == early.atr14
    again = build(bars[:50] + [mk(99, 1, 99999, 1, 5, start=RTH_O + 300 * 50)][:0])
    assert again == early
    # 在后面追加 K 线后，较早时点能看到的 swing 不变
    visible_then = {(s.kind, s.bar_start_utc) for s in F.swings(bars[:50])}
    visible_later = {(s.kind, s.bar_start_utc) for s in F.swings(bars) if s.known_at_bar_start_utc <= bars[49].start_utc}
    assert visible_then == visible_later
