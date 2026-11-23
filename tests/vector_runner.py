"""逐 bar 策略向量的运行器。

向量是 YAML：
    name, description, instrument (ES|NQ…), date, start ("HH:MM" ET，第一根的起始时刻),
    atr (点数；null 表示不可用), adr ({points, valid_days} 或 null), mirror_axis (点数，缺省 5000),
    bars: [{b: [o, h, l, c], repeat: n, trades: [{exit, net}], expect: {...}}]

expect 的键（价格都是点数）：signal（缺省 WAIT）、label、side、trigger、limit、stop、target ([kind, value])、
skip、cycle、spike (true/false)、phase、spike_extreme、ended。没写的键不检查；没写 expect 的 K 线要求不是 SETUP_VALID。

镜像：把全部价格关于 mirror_axis 翻转，期望里的方向、H/L 标签、BULL/BEAR 互换、价位翻转，再跑一遍。
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import yaml

from trading_agent import features, sessions
from trading_agent.strategies.base import FIXED_SCALE, Adr, Bar, ClosedTrade, FeatureContext, StrategyState

VECTOR_DIR = Path(__file__).parent / "vectors" / "strategy" / "pa_brooks"
TICK = Decimal("0.25")
CONTRACTS = {"ES": "ESZ2025", "MES": "MESZ2025", "NQ": "NQZ2025", "MNQ": "MNQZ2025"}
SWAP = {"LONG": "SHORT", "SHORT": "LONG", "BULL_SPIKE": "BEAR_SPIKE", "BEAR_SPIKE": "BULL_SPIKE",
        "BULL_MICROCHANNEL": "BEAR_MICROCHANNEL", "BEAR_MICROCHANNEL": "BULL_MICROCHANNEL"}


def ticks(points) -> int:
    q = Decimal(str(points)) / TICK
    assert q == q.to_integral_value(), f"{points} 不在 tick 上"
    return int(q)


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        v = yaml.safe_load(f)
    v["_path"] = path.name
    return v


def all_vectors() -> list[Path]:
    return sorted(VECTOR_DIR.glob("*.yaml"))


def _mirror_label(label):
    return None if label is None else {"H": "L", "L": "H"}[label[0]] + label[1:]


def expand(vec: dict, mirror: bool = False) -> list[dict]:
    """展开 repeat，换算成 tick，并按需镜像。返回 [{ohlc, trades, expect}]。"""
    axis = ticks(vec.get("mirror_axis", 5000))
    flip = (lambda t: 2 * axis - t) if mirror else (lambda t: t)
    out = []
    for item in vec["bars"]:
        o, h, l, c = (ticks(x) for x in item["b"])
        ohlc = (flip(o), flip(l), flip(h), flip(c)) if mirror else (o, h, l, c)
        exp = dict(item.get("expect") or {})
        for k in ("trigger", "limit", "stop", "spike_extreme"):
            if exp.get(k) is not None:
                exp[k] = flip(ticks(exp[k]))
        if mirror:
            for k in ("side", "cycle"):
                if exp.get(k) in SWAP:
                    exp[k] = SWAP[exp[k]]
            if "label" in exp:
                exp["label"] = _mirror_label(exp["label"])
        for _ in range(int(item.get("repeat", 1))):
            out.append({"ohlc": ohlc, "trades": item.get("trades") or [], "expect": exp})
    return out


def contexts(vec: dict, mirror: bool = False):
    """逐根产出 (序号, FeatureContext, 期望)。"""
    day = date.fromisoformat(str(vec["date"]))
    hh, mm = (int(x) for x in str(vec["start"]).split(":"))
    t0 = sessions.ts(datetime.combine(day, time(hh, mm), sessions.NY))
    rth_open, rth_close = sessions.rth_bounds(day)
    root = vec.get("instrument", "ES")
    atr = None if vec.get("atr") is None else int(Decimal(str(vec["atr"])) / TICK * FIXED_SCALE)
    adr = None
    if vec.get("adr"):
        adr = Adr(Fraction(ticks(vec["adr"]["points"])), int(vec["adr"]["valid_days"]))
    bars: list[Bar] = []
    trades: list[ClosedTrade] = []
    for i, item in enumerate(expand(vec, mirror)):
        start = t0 + 300 * i
        d, sess = sessions.classify(start)
        o, h, l, c = item["ohlc"]
        bars.append(Bar(start, start + 300, o, h, l, c, 100, sess, d.isoformat()))
        trades += [ClosedTrade(f"t{len(trades) + j}", t["exit"], int(t["net"])) for j, t in enumerate(item["trades"])]
        ctx = FeatureContext(
            scope=f"pa_brooks:{root}:RTH:5m", instrument_root=root, contract_id=CONTRACTS[root],
            as_of_utc=start + 305, bars=tuple(bars[-64:]), rth_open_utc=rth_open, rth_close_utc=rth_close,
            atr14=atr, rolling_20_range=features.rolling_range(bars),
            intraday_rth_range=features.intraday_rth_range(bars, rth_open, rth_close), adr10=adr,
            closed_trades=tuple(trades), known_at_utc=start + 305)
        yield i, ctx, item["expect"]


def observed(result) -> dict:
    m = result.decision_metadata
    sp = m.structure["spike"]
    it = result.order_intent
    return {
        "signal": result.signal_status, "label": m.label, "side": m.side, "skip": m.skip_reason,
        "trigger": None if it is None else it.trigger_ticks, "limit": None if it is None else it.limit_ticks,
        "stop": None if it is None else it.stop_ticks,
        "target": None if it is None else [it.target.kind, it.target.value],
        "cycle": m.structure["cycle"], "spike": bool(sp) and not sp["ended"],
        "phase": sp["phase"] if sp else None, "spike_extreme": sp["extreme_ticks"] if sp else None,
        "ended": sp["ended"] if sp else None,
    }


def run(strategy, vec: dict, mirror: bool = False, roundtrip_state: bool = False) -> list:
    """跑一个向量，逐根核对期望。返回每一根的结果。期望不符时指出是哪一根、哪个字段。"""
    state = strategy.initial_state()
    results = []
    for i, ctx, exp in contexts(vec, mirror):
        if roundtrip_state:
            state = StrategyState.from_json(state.to_json())
        res = strategy.on_bar(ctx, state)
        state = res.next_state
        results.append(res)
        got = observed(res)
        want = {"signal": "WAIT", **exp}
        for k, v in want.items():
            assert got[k] == v, (f"{vec['_path']}{'（镜像）' if mirror else ''} 第 {i} 根 [{k}]："
                                 f"期望 {v!r}，得到 {got[k]!r}（skip={got['skip']!r}, label={got['label']!r}）")
    return results
