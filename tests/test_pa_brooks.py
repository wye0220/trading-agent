import copy
from datetime import date
from fractions import Fraction

import pytest
import yaml

import helpers as H
import vector_runner as V
from trading_agent import sessions
from trading_agent.strategies import pa_brooks as PA
from trading_agent.strategies.base import (FIXED_SCALE as S, Bar, ClosedTrade, FeatureContext, PriceRange,
                                           StrategyState, TraderPlan)

STRAT = PA.PaBrooksStrategy.from_default_config()
CFG = STRAT.config
RAW = yaml.safe_load(PA.DEFAULT_CONFIG.read_text(encoding="utf-8"))
VECTORS = V.all_vectors()
ATR4 = 16 * S                    # 4 点 = 16 tick


def vec(name):
    return V.load(V.VECTOR_DIR / f"{name}.yaml")


# ---------------------------------------------------------------- 8.1 / 8.2 向量
def test_vector_inventory():
    assert len(VECTORS) >= 24


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_vector_forward(path):
    V.run(STRAT, V.load(path))


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_vector_mirrored(path):
    V.run(STRAT, V.load(path), mirror=True)


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_vector_with_state_roundtrip_every_bar(path):
    """每一根之前都把状态序列化再恢复，结果与不中断时逐根相同。"""
    v = V.load(path)
    a = V.run(STRAT, v)
    b = V.run(STRAT, v, roundtrip_state=True)
    assert [(r.signal_status, r.order_intent, r.decision_metadata.to_dict(), r.next_state) for r in a] == [
        (r.signal_status, r.order_intent, r.decision_metadata.to_dict(), r.next_state) for r in b]


def test_runner_reports_bar_and_field():
    v = vec("spike_exactly_2_5_atr")
    v["bars"][2]["expect"]["cycle"] = "BEAR_SPIKE"
    with pytest.raises(AssertionError, match=r"spike_exactly_2_5_atr\.yaml 第 2 根 \[cycle\]：期望 'BEAR_SPIKE'，得到 'BULL_SPIKE'"):
        V.run(STRAT, v)


def test_mirror_really_flips_direction():
    v = vec("spike_pullback_h1_h2_resume")
    fwd, mir = V.run(STRAT, v), V.run(STRAT, v, mirror=True)
    axis = V.ticks(5000)
    pairs = [(a, b) for a, b in zip(fwd, mir) if a.order_intent]
    assert len(pairs) == 2
    for a, b in pairs:
        ia, ib = a.order_intent, b.order_intent
        assert (ia.side, ib.side) == ("LONG", "SHORT")
        assert (a.decision_metadata.label[0], b.decision_metadata.label[0]) == ("H", "L")
        assert ib.trigger_ticks == 2 * axis - ia.trigger_ticks and ib.limit_ticks == 2 * axis - ia.limit_ticks
        assert ib.stop_ticks == 2 * axis - ia.stop_ticks and ib.target == ia.target
    assert [r.signal_status for r in fwd] == [r.signal_status for r in mir]
    assert mir[2].decision_metadata.structure["cycle"] == "BEAR_SPIKE"


# ---------------------------------------------------------------- 4.1 / 4.2 配置
def test_default_config_values():
    assert CFG.version == "v1.0" and CFG.timeframe == "5m"
    assert (CFG.spike_min_bars, CFG.spike_body_min_pct, CFG.spike_close_divisor) == (2, 50, 3)
    assert CFG.spike_min_move_atr == Fraction(5, 2)
    assert (CFG.quality_body_min_pct, CFG.quality_max_range_atr) == (20, 3)
    assert (CFG.trigger_offset, CFG.limit_offset, CFG.stop_offset, CFG.ttl_bars) == (1, 1, 1, 1)
    assert CFG.adr_fraction == Fraction(2, 5) and CFG.fallback_risk_multiple == 2 and CFG.late_session_2r is False
    assert (CFG.tail_window_minutes, CFG.max_stops_per_day, CFG.max_label) == (30, 2, 3)
    assert CFG.group("ES") == CFG.group("MES") == {"max_retrace_pct": 75, "max_pullback_bars": 20}
    assert CFG.group("NQ") == CFG.group("MNQ") == {"max_retrace_pct": 60, "max_pullback_bars": 12}
    assert [s["name"] for s in CFG.setups] == list(PA.KNOWN_SETUPS)         # 优先级从高到低
    assert CFG.enabled_setups == ("spike_pullback",)
    assert set(CFG.setup("spike_pullback")["exempt"]) == {"h1l1_block", "counter_htf", "range_position", "exhaustion"}
    with pytest.raises(PA.StrategyConfigError, match="没有品种 CL"):
        CFG.group("CL")


def changed(path, value):
    raw = copy.deepcopy(RAW)
    node = raw
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return raw


def test_enabling_unimplemented_setup_fails():
    raw = copy.deepcopy(RAW)
    next(s for s in raw["setups"] if s["name"] == "wedge")["enabled"] = True
    with pytest.raises(PA.StrategyConfigError, match="形态 wedge 尚未实现，不能启用"):
        PA.parse_config(raw)


@pytest.mark.parametrize("path,value,needle", [
    (("params", "spike", "min_move_atr"), "-1", "min_move_atr"),
    (("params", "spike", "min_move_atr"), "abc", "min_move_atr"),
    (("params", "spike", "min_bars"), 1, "min_bars"),
    (("params", "quality", "body_min_pct"), 150, "body_min_pct"),
    (("params", "quality", "body_min_pct"), "20", "body_min_pct"),
    (("params", "entry", "order_type"), "MARKET_NEXT_OPEN", "order_type"),
    (("params", "target", "late_session_2r"), True, "late_session_2r"),
    (("params", "discipline", "max_stops_per_day"), 0, "max_stops_per_day"),
    (("groups", "ES_MES", "max_retrace_pct"), 0, "max_retrace_pct"),
    (("instruments", "ES"), "NOPE", "未定义的组"),
    (("strategy_family",), "orb", "strategy_family"),
    (("params", "spike", "typo_key"), 1, "未知的键"),
])
def test_invalid_config_rejected(path, value, needle):
    with pytest.raises(PA.StrategyConfigError, match=needle):
        PA.parse_config(changed(path, value))


def test_config_structure_errors():
    raw = copy.deepcopy(RAW)
    raw["setups"] = raw["setups"][:-1]
    with pytest.raises(PA.StrategyConfigError, match="缺少形态"):
        PA.parse_config(raw)
    raw = copy.deepcopy(RAW)
    raw["setups"][0]["exempt"] = ["counter_spike"]
    with pytest.raises(PA.StrategyConfigError, match="不可豁免"):
        PA.parse_config(raw)
    raw = copy.deepcopy(RAW)
    raw["setups"][0]["exempt"] = ["nope"]
    with pytest.raises(PA.StrategyConfigError, match="未知的过滤器"):
        PA.parse_config(raw)
    raw = copy.deepcopy(RAW)
    del raw["params"]["quality"]
    with pytest.raises(PA.StrategyConfigError, match="缺少键"):
        PA.parse_config(raw)


# ---------------------------------------------------------------- 5.1 / 5.4 基本判定
def B(o, h, l, c, i=0):
    return Bar(1000 + 300 * i, 1300 + 300 * i, o, h, l, c, 1, "RTH", "2025-11-25")


def test_strong_trend_bar_rules():
    prev = B(100, 104, 96, 100)
    assert PA.is_strong_trend_bar(B(100, 112, 100, 110, 1), prev, 1, CFG)             # 实体 10/12，收在上 1/3
    assert not PA.is_strong_trend_bar(B(100, 112, 100, 110, 1), None, 1, CFG)
    assert not PA.is_strong_trend_bar(B(100, 104, 92, 103, 1), prev, 1, CFG)          # 没创新高
    assert not PA.is_strong_trend_bar(B(100, 112, 100, 105, 1), prev, 1, CFG)         # 实体 5/12 < 50%
    assert PA.is_strong_trend_bar(B(100, 112, 100, 108, 1), prev, 1, CFG)             # 收盘恰在上 1/3 边界
    assert not PA.is_strong_trend_bar(B(100, 112, 100, 107, 1), prev, 1, CFG)
    assert PA.is_strong_trend_bar(B(102, 112, 100, 108, 1), prev, 1, CFG)             # 实体恰好 50%
    assert not PA.is_strong_trend_bar(B(103, 112, 100, 108, 1), prev, 1, CFG)
    assert not PA.is_strong_trend_bar(B(110, 110, 110, 110, 1), prev, 1, CFG)         # 零全幅
    assert PA.is_strong_trend_bar(B(100, 100, 88, 90, 1), prev, -1, CFG)              # 熊镜像
    assert not PA.is_strong_trend_bar(B(100, 100, 88, 90, 1), prev, 1, CFG)


def test_signal_bar_quality_rules():
    q = lambda b, atr=ATR4: PA.signal_bar_quality(b, atr, CFG)[:2]      # noqa: E731
    assert q(B(100, 110, 100, 102)) == (True, None)                       # 实体恰好 20%
    assert q(B(100, 120, 100, 103)) == (False, "QUALITY_BODY_TOO_SMALL")  # 15%
    assert q(B(100, 148, 100, 140)) == (True, None)                       # 全幅恰好 3×ATR = 48 tick
    assert q(B(100, 149, 100, 140)) == (False, "QUALITY_RANGE_TOO_LARGE")
    assert q(B(100, 100, 100, 100)) == (False, "QUALITY_ZERO_RANGE")
    ok, why, ev = PA.signal_bar_quality(B(100, 120, 100, 103), ATR4, CFG)
    assert ev == {"range_ticks": 20, "body_ticks": 3, "atr14_fixed": ATR4}


# ---------------------------------------------------------------- 6.1 过滤器
def fctx(bars, **kw):
    return FeatureContext(scope="pa_brooks:ES:RTH:5m", instrument_root="ES", contract_id="ESZ2025", as_of_utc=1,
                          bars=tuple(bars), rth_open_utc=0, rth_close_utc=10**10, atr14=ATR4, **kw)


def filters(cand, ctx, spike=None, htf="UNKNOWN", cfg=CFG):
    first, res = PA.evaluate_filters(cand, ctx, spike, htf, cfg)
    return first, {r["filter"]: r for r in res}


def generic_cfg():
    """把一个没有任何豁免的形态借给过滤器测试用（只用于调用 evaluate_filters，不会生成订单）。"""
    return CFG


def test_filter_order_and_first_reason():
    bar = B(100, 104, 96, 103)
    cand = {"setup": "h1_l1", "d": 1, "label": "H1", "bar": bar}           # h1_l1 没有任何豁免
    ctx = fctx([B(100, 104, 96, 100), bar], rolling_20_range=PriceRange(104, 0))
    first, r = filters(cand, ctx, spike={"d": -1}, htf="BEAR")
    assert list(r) == ["h1l1_block", "counter_spike", "counter_htf", "range_position", "exhaustion"]
    assert first == "H1L1_BLOCKED"                                          # 顺序在前的那个
    assert r["counter_spike"]["triggered"] and r["counter_htf"]["triggered"] and r["range_position"]["triggered"]
    assert all(not x["exempt"] for x in r.values())


def test_each_filter_triggers_and_not():
    bar = B(100, 104, 96, 103)
    base = [B(100, 104, 96, 100), bar]
    ok_range = PriceRange(200, 90)                    # 信号 K 低点 96 在下 1/3（90..126.67）
    c = lambda label="H2", d=1: {"setup": "h1_l1", "d": d, "label": label, "bar": bar}       # noqa: E731
    # 1) H1/L1
    assert filters(c("H1"), fctx(base, rolling_20_range=ok_range), htf="BULL")[0] == "H1L1_BLOCKED"
    assert filters(c("L1", -1), fctx(base, rolling_20_range=PriceRange(110, 0)), htf="BEAR")[0] == "H1L1_BLOCKED"
    assert filters(c("H2"), fctx(base, rolling_20_range=ok_range), htf="BULL")[0] is None
    # 2) 尖峰内逆势
    assert filters(c(), fctx(base, rolling_20_range=ok_range), spike={"d": -1}, htf="BULL")[0] == "COUNTER_SPIKE"
    assert filters(c(), fctx(base, rolling_20_range=ok_range), spike={"d": 1}, htf="BULL")[0] is None
    # 3) 逆 1 小时方向；方向未知也等待
    assert filters(c(), fctx(base, rolling_20_range=ok_range), htf="BEAR")[0] == "COUNTER_HTF"
    assert filters(c(), fctx(base, rolling_20_range=ok_range), htf="UNKNOWN")[0] == "HTF_DIRECTION_UNKNOWN"
    assert filters(c(d=-1), fctx(base, rolling_20_range=PriceRange(110, 0)), htf="BEAR")[0] is None
    # 4) 区间位置：做多的低点须在下 1/3，做空的高点须在上 1/3
    assert filters(c(), fctx(base, rolling_20_range=PriceRange(200, 0)), htf="BULL")[0] == "RANGE_POSITION"   # 96 > 66.7
    assert filters(c(), fctx(base, rolling_20_range=PriceRange(288, 0)), htf="BULL")[0] is None               # 96 = 288/3
    assert filters(c(d=-1), fctx(base, rolling_20_range=PriceRange(200, 0)), htf="BEAR")[0] == "RANGE_POSITION"
    assert filters(c(), fctx(base), htf="BULL")[0] == "FEATURE_UNAVAILABLE:rolling_20_range"
    # 5) 衰竭：连续 ≥10 根在 EMA 上方，且全幅 ≥1.5×ATR（24 tick）或为近 10 根最大
    wide = B(100, 124, 100, 120)
    many = [B(100, 130, 100, 110, i) for i in range(10)]
    cw = {"setup": "h1_l1", "d": 1, "label": "H2", "bar": wide}
    assert filters(cw, fctx(many + [wide], rolling_20_range=PriceRange(400, 100), ema_side_run=10), htf="BULL")[0] == "EXHAUSTION"
    assert filters(cw, fctx(many + [wide], rolling_20_range=PriceRange(400, 100), ema_side_run=9), htf="BULL")[0] is None
    assert filters(cw, fctx(many + [wide], rolling_20_range=PriceRange(400, 100), ema_side_run=-10), htf="BULL")[0] is None
    small = B(100, 108, 100, 106)
    cs = {"setup": "h1_l1", "d": 1, "label": "H2", "bar": small}
    assert filters(cs, fctx(many + [small], rolling_20_range=PriceRange(400, 100), ema_side_run=12), htf="BULL")[0] is None
    tiny = [B(100, 104, 100, 102, i) for i in range(10)]
    assert filters(cs, fctx(tiny + [small], rolling_20_range=PriceRange(400, 100), ema_side_run=12), htf="BULL")[0] == "EXHAUSTION"


def test_spike_pullback_exemptions():
    """Spike 回调的 H1 在 1 小时方向未知、区间位置不利、衰竭条件满足时都不被拦截。"""
    wide = B(100, 124, 100, 120)
    many = [B(100, 130, 100, 110, i) for i in range(10)]
    cand = {"setup": "spike_pullback", "d": 1, "label": "H1", "bar": wide}
    ctx = fctx(many + [wide], rolling_20_range=PriceRange(124, 0), ema_side_run=15)
    first, r = filters(cand, ctx, spike={"d": 1}, htf="UNKNOWN")
    assert first is None
    for name in ("h1l1_block", "counter_htf", "range_position", "exhaustion"):
        assert r[name]["triggered"] and r[name]["exempt"], name            # 证据照样记录
    assert not r["counter_spike"]["triggered"] and not r["counter_spike"]["exempt"]
    assert filters(cand, ctx, spike={"d": -1})[0] == "COUNTER_SPIKE"        # 这一条不豁免


def test_filter_evidence_in_metadata():
    res = V.run(STRAT, vec("spike_pullback_h1_h2_resume"))[5]
    f = {x["filter"]: x for x in res.decision_metadata.evidence["filters"]}
    assert f["h1l1_block"]["triggered"] and f["h1l1_block"]["exempt"]
    assert f["counter_htf"]["reason"] == "HTF_DIRECTION_UNKNOWN" and f["counter_htf"]["exempt"]
    assert res.decision_metadata.skip_reason is None


# ---------------------------------------------------------------- 6.3 / 6.4 纪律与特征不可用
def run_ctxs(v, state=None, upto=None, tweak=None):
    state = state or STRAT.initial_state()
    out = []
    for i, ctx, _ in V.contexts(v):
        if upto is not None and i > upto:
            break
        if tweak:
            ctx = tweak(i, ctx)
        res = STRAT.on_bar(ctx, state)
        state = res.next_state
        out.append((ctx, res))
    return out, state


def test_next_day_resumes_after_halt():
    v = vec("two_stops_halt")
    out, state = run_ctxs(v)
    assert out[-1][1].decision_metadata.skip_reason == "TWO_STOPS_HALT" and state.data["halted"] is True
    v2 = vec("spike_pullback_h1_h2_resume")
    v2["date"] = "2025-11-26"
    out2, state2 = run_ctxs(v2, state=state, upto=5)
    assert out2[5][1].signal_status == "SETUP_VALID" and state2.data["halted"] is False and state2.data["stops"] == 0
    assert state2.data["day"] == "2025-11-26"


def test_atr_unavailable_waits():
    v = vec("spike_pullback_h1_h2_resume")
    v["atr"] = None
    out, _ = run_ctxs(v)
    for _, res in out:
        m = res.decision_metadata
        assert res.signal_status == "WAIT" and m.skip_reason == "FEATURE_UNAVAILABLE:atr14"
        assert m.structure["spike"] is None and "ATR14" in m.next_confirmation


def test_eth_bars_are_not_scanned():
    v = vec("spike_pullback_h1_h2_resume")
    v["start"] = "08:00"
    out, _ = run_ctxs(v)
    assert {r.decision_metadata.skip_reason for _, r in out} == {"OUTSIDE_SCAN_SESSION"}
    assert all(r.signal_status == "WAIT" for _, r in out)


# ---------------------------------------------------------------- 6.5 订单意图
def test_short_intent_numbers():
    """L1：信号 K 最低 5000.00，回调最高 5010.00 → 触发 4999.75，限价 4999.50，止损 5010.25。"""
    v = {"_path": "inline", "instrument": "ES", "date": "2025-11-25", "start": "10:00", "atr": 4.0, "adr": None,
         "bars": [{"b": [5020, 5021, 5019, 5019.75]},
                  {"b": [5019.75, 5020, 5014, 5014.25]},
                  {"b": [5014.25, 5014.5, 4995, 4995.5]},          # 熊 Spike：首根开盘 5019.75，极值 4995
                  {"b": [4995.5, 5004, 4995.25, 5003.5]},          # 回调第 1 根
                  {"b": [5003.5, 5010, 5003.25, 5009.5]},          # 破前高：反向腿；回调最高 5010
                  {"b": [5009.5, 5009.75, 5000, 5000.5],
                   "expect": {"signal": "SETUP_VALID", "label": "L1", "side": "SHORT", "trigger": 4999.75,
                              "limit": 4999.5, "stop": 5010.25, "target": ["INITIAL_RISK_MULTIPLE", 2]}}]}
    res = V.run(STRAT, v)[-1]
    assert res.order_intent.order_type == "STOP_LIMIT_NEXT_BAR" and res.order_intent.ttl_bars == 1


@pytest.mark.parametrize("adr_ticks,want", [(201, 81), (200, 80), (1, 1), (2, 1), (3, 2), (Fraction(601, 3), 81)])
def test_adr_target_rounds_up(adr_ticks, want):
    from trading_agent.strategies.base import Adr
    ctx = fctx([B(1, 2, 0, 1)], adr10=Adr(Fraction(adr_ticks), 5))
    t, rule, ev = PA._target(ctx, ctx.bar, CFG)
    assert (t.kind, t.value, rule, ev["adr_valid_days"]) == ("OFFSET_FROM_FILL_TICKS", want, "ADR_FRACTION", 5)


def test_target_falls_back_without_adr_or_outside_rth():
    from trading_agent.strategies.base import Adr
    ctx = fctx([B(1, 2, 0, 1)])
    t, rule, ev = PA._target(ctx, ctx.bar, CFG)
    assert (t.kind, t.value, rule, ev["adr_valid_days"]) == ("INITIAL_RISK_MULTIPLE", 2, "RISK_MULTIPLE_FALLBACK", 0)
    eth = Bar(1, 301, 1, 2, 0, 1, 1, "ETH", "2025-11-25")
    t, rule, _ = PA._target(fctx([eth], adr10=Adr(Fraction(200), 9)), eth, CFG)
    assert t.kind == "INITIAL_RISK_MULTIPLE"


def test_setup_ids():
    a = V.run(STRAT, vec("spike_pullback_h1_h2_resume"))
    b = V.run(STRAT, vec("spike_pullback_h1_h2_resume"))
    h1, h2 = a[5].order_intent.setup_id, a[9].order_intent.setup_id
    assert h1 != h2 and len(h1) == 16 and (h1, h2) == (b[5].order_intent.setup_id, b[9].order_intent.setup_id)
    end = a[5].decision_metadata.signal_bar_end_utc
    assert h1 == PA.make_setup_id("pa_brooks", "ESZ2025", "spike_pullback:H1", end, "v1.0")
    assert h1 != PA.make_setup_id("pa_brooks", "ESH2026", "spike_pullback:H1", end, "v1.0")
    assert h1 != PA.make_setup_id("pa_brooks", "ESZ2025", "spike_pullback:H1", end, "v1.1")


# ---------------------------------------------------------------- 6.6 周期
def test_cycle_outputs():
    out, _ = run_ctxs(vec("spike_pullback_h1_h2_resume"))
    cyc = [r.decision_metadata.structure["cycle"] for _, r in out]
    assert cyc[2:10] == ["BULL_SPIKE"] * 8 and cyc[0] == "UNKNOWN"
    assert all(r.decision_metadata.structure["htf_direction"] == "UNKNOWN" for _, r in out)
    # 微通道：连续 5 根不破前一根低点
    up = {"_path": "m", "instrument": "ES", "date": "2025-11-25", "start": "10:00", "atr": 40.0, "adr": None,
          "bars": [{"b": [5000 + i, 5001.5 + i, 4999.75 + i, 5000.5 + i]} for i in range(7)]}
    res = V.run(STRAT, up)
    assert [r.decision_metadata.structure["cycle"] for r in res][4:] == ["UNKNOWN", "BULL_MICROCHANNEL", "BULL_MICROCHANNEL"]
    mres = V.run(STRAT, up, mirror=True)
    assert mres[-1].decision_metadata.structure["cycle"] == "BEAR_MICROCHANNEL"


# ---------------------------------------------------------------- 7.1 / 7.2 输出
def test_decision_metadata_fields():
    res = V.run(STRAT, vec("spike_pullback_h1_h2_resume"))[5]
    m = res.decision_metadata
    d = m.to_dict()
    assert d["strategy_family"] == "pa_brooks" and d["contract_id"] == "ESZ2025" and d["strategy_version"] == "v1.0"
    assert d["signal_bar_end_utc"] == H.et(date(2025, 11, 25), 10, 30) and d["known_at_utc"] == d["signal_bar_end_utc"] + 5
    assert d["valid_until_utc"] == d["signal_bar_end_utc"] + 300                      # 只在下一根内有效
    assert (d["setup"], d["label"], d["side"], d["skip_reason"]) == ("spike_pullback", "H1", "LONG", None)
    assert (d["entry_trigger_ticks"], d["entry_limit_ticks"], d["stop_ticks"]) == (20041, 20042, 20027)
    assert d["target"] == {"kind": "OFFSET_FROM_FILL_TICKS", "value": 81} and d["target_rule"] == "ADR_FRACTION"
    st = d["structure"]
    assert st["cycle"] == "BULL_SPIKE" and st["htf_direction"] == "UNKNOWN"
    sp = st["spike"]
    assert (sp["direction"], sp["phase"], sp["bars"]) == ("BULL", "PULLBACK", 2)
    assert (sp["first_open_ticks"], sp["extreme_ticks"], sp["move_ticks"]) == (20001, 20048, 45)
    assert sp["atr14_fixed"] == ATR4 and sp["pullback_bars"] == 3 and sp["pullback_bucket"] == "1-3"
    assert sp["pullback_extreme_ticks"] == 20028 and sp["start_utc"] < sp["end_utc"]
    ev = d["evidence"]
    assert ev["signal_bar_quality"] == {"range_ticks": 11, "body_ticks": 9, "atr14_fixed": ATR4}
    assert ev["target"]["adr_valid_days"] == 10 and len(ev["filters"]) == 5
    assert ev["signal_bar"]["high"] == 20040 and d["next_confirmation"]
    assert res.order_intent.evidence["pullback_extreme_ticks"] == 20028


def test_pullback_buckets():
    assert [PA._bucket(n) for n in (1, 3, 4, 9, 10, 20)] == ["1-3", "1-3", "4-9", "4-9", "10+", "10+"]


def test_render_zh_setup_valid():
    res = V.run(STRAT, vec("spike_pullback_h1_h2_resume"))[5]
    text = PA.render_zh(res, "0.25")
    lines = text.splitlines()
    assert lines[0].startswith("新信号：做多 Spike 回调 H1，SETUP_VALID。")
    assert "触发 5010.25" in lines[0] and "限价 5010.50" in lines[0] and "止损 5006.75" in lines[0]
    assert "成交价+20.25 点（0.40×ADR）" in lines[0] and "有效至 10:35 ET" in lines[0]
    assert lines[1].startswith("周期：BULL_SPIKE（牛 Spike 2 根，极值 5012.00，回调第 3 根，回调极值 5007.00）")
    assert lines[2] == "最新 K 线（2025-11-25 10:30 ET 收）：开 5007.50 高 5010.00 低 5007.25 收 5009.75。"
    assert lines[-2].startswith("下一确认条件：") and lines[-1] == PA.DISCLAIMER


def test_render_zh_blocked_and_waiting():
    blocked = V.run(STRAT, vec("tail_1535_blocked"))[5]
    t = PA.render_zh(blocked, "0.25")
    assert t.splitlines()[0] == "新信号：做多 Spike 回调 H1 被拦截，WAIT。原因：TAIL_WINDOW_UNCONFIGURED。"
    assert t.splitlines()[-1] == PA.DISCLAIMER
    halted = V.run(STRAT, vec("two_stops_halt"))[5]
    assert "纪律：今日已连续止损 2 次，停手。" in PA.render_zh(halted, "0.25")
    waiting = V.run(STRAT, vec("spike_below_2_5_atr"))[3]
    w = PA.render_zh(waiting, "0.25").splitlines()
    assert w[0] == "新信号：无，WAIT。" and w[1] == "周期：UNKNOWN；1 小时方向 UNKNOWN。"
    assert "等待至少 2 根连续强趋势 K" in w[-2] and w[-1] == PA.DISCLAIMER
    short = V.run(STRAT, vec("spike_pullback_h1_h2_resume"), mirror=True)[5]
    s0 = PA.render_zh(short, "0.25").splitlines()[0]
    assert s0.startswith("新信号：做空 Spike 回调 L1") and "成交价-20.25 点" in s0


# ---------------------------------------------------------------- 7.3 人工计划一致性
def test_alignment_countertrend_in_bear_spike():
    v = vec("spike_pullback_h1_h2_resume")
    out, _ = run_ctxs(v, upto=4)                      # 牛 Spike 的回调中，还没有信号
    ctx, res = out[-1]
    r = STRAT.evaluate_pretrade_alignment(ctx, res.next_state, TraderPlan("SHORT", 20030, 20060))
    assert r.alignment == "DEVIATES" and r.flags == ("COUNTERTREND_BEFORE_CONFIRMATION",)
    assert r.evidence["spike_direction"] == "BULL" and r.setup_id is None
    # 熊 Spike 中抄底（镜像）
    state = STRAT.initial_state()
    for i, c, _ in V.contexts(v, mirror=True):
        res2 = STRAT.on_bar(c, state)
        state = res2.next_state
        if i == 4:
            r2 = STRAT.evaluate_pretrade_alignment(c, state, TraderPlan("LONG", 19970))
            assert r2.alignment == "DEVIATES" and set(r2.flags) == {"NO_STOP", "COUNTERTREND_BEFORE_CONFIRMATION"}
            assert r2.evidence["spike_direction"] == "BEAR"
            break


def test_alignment_with_active_signal():
    out, _ = run_ctxs(vec("spike_pullback_h1_h2_resume"), upto=5)
    ctx, res = out[-1]
    sid = res.order_intent.setup_id
    r = STRAT.evaluate_pretrade_alignment(ctx, res.next_state, TraderPlan("LONG", 20041, 20027))
    assert (r.alignment, r.setup_id, r.flags) == ("ALIGNED", sid, ())
    wide = STRAT.evaluate_pretrade_alignment(ctx, res.next_state, TraderPlan("LONG", 20050, 20000))
    assert wide.alignment == "ALIGNED" and set(wide.flags) == {"STOP_WIDER_THAN_SETUP", "ENTRY_BEYOND_SETUP_LIMIT"}
    nostop = STRAT.evaluate_pretrade_alignment(ctx, res.next_state, TraderPlan("LONG", 20041))
    assert nostop.alignment == "ALIGNED" and nostop.flags == ("NO_STOP",)


def test_alignment_unknown_cases():
    out, _ = run_ctxs(vec("spike_below_2_5_atr"))
    ctx, res = out[-1]
    r = STRAT.evaluate_pretrade_alignment(ctx, res.next_state, TraderPlan("LONG", 20000))
    assert r.alignment == "UNKNOWN" and r.flags == ("NO_STOP",)                       # 没有可判断的结构
    r0 = STRAT.evaluate_pretrade_alignment(ctx, STRAT.initial_state(), TraderPlan("SHORT", 20000, 20010))
    assert (r0.alignment, r0.flags) == ("UNKNOWN", ())
    out, _ = run_ctxs(vec("spike_pullback_h1_h2_resume"), upto=6)                     # 信号已过期一根
    ctx, res = out[-1]
    same = STRAT.evaluate_pretrade_alignment(ctx, res.next_state, TraderPlan("LONG", 20041, 20027))
    assert same.alignment == "UNKNOWN" and same.flags == ("NO_ACTIVE_SETUP",)


# ---------------------------------------------------------------- 8.3 纯函数性（行为）
def test_on_bar_is_pure_and_deterministic():
    v = vec("spike_pullback_h1_h2_resume")
    state = STRAT.initial_state()
    for i, ctx, _ in V.contexts(v):
        before_state, before_ctx = state.to_json(), repr(ctx)
        a = STRAT.on_bar(ctx, state)
        b = STRAT.on_bar(ctx, state)
        assert state.to_json() == before_state and repr(ctx) == before_ctx       # 输入没有被修改
        assert (a.signal_status, a.order_intent, a.next_state) == (b.signal_status, b.order_intent, b.next_state)
        assert a.decision_metadata.to_dict() == b.decision_metadata.to_dict()
        state = a.next_state


def test_unsupported_state_version_rejected():
    from trading_agent.strategies.base import ContractViolation
    _, ctx, _ = next(V.contexts(vec("spike_exactly_2_5_atr")))
    with pytest.raises(ContractViolation, match="状态结构版本"):
        STRAT.on_bar(ctx, StrategyState(99, {}))
    assert STRAT.initial_state().state_schema_version == PA.STATE_SCHEMA_VERSION == 1
