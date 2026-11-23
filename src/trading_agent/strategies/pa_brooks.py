"""PA v1.0：Al Brooks 价格行为，5 分钟。纯函数，无 I/O（配置由调用方加载后传入）。

只有 Spike 回调会生成订单意图。其余形态登记了优先级和开关，但未实现、未启用。
规格见 docs/strategy-spec.md。

内部用"定向"坐标处理多空：做多方向 d=+1，做空 d=-1。把价格乘以 d、并在做空时交换高低，
就能用同一套"牛"逻辑处理两个方向，多空对称由构造保证。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .base import (FIXED_SCALE, AlignmentResult, Bar, ContractViolation, DecisionMetadata, FeatureContext,
                   FeatureRequirements, OrderIntent, StrategyState, StrategyStepResult, TargetSpec, TraderPlan)

FAMILY = "pa_brooks"
STATE_SCHEMA_VERSION = 1
DISCLAIMER = "以上为规则化的价格行为学习标注，不构成投资建议。期货有杠杆，风险自负。"
IMPLEMENTED_SETUPS = frozenset({"spike_pullback"})
KNOWN_SETUPS = ("climax_reversal", "spike_pullback", "major_trend_reversal", "wedge", "failed_breakout",
                "double_top_bottom", "lower_high_higher_low", "flag", "h2_l2", "h1_l1")
FILTERS = ("h1l1_block", "counter_spike", "counter_htf", "range_position", "exhaustion")
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "strategies" / "pa-esnq-5m-v1.0.yaml"


class StrategyConfigError(ValueError):
    pass


# ---------------------------------------------------------------- 配置
@dataclass(frozen=True)
class PaConfig:
    version: str
    timeframe: str
    spike_min_bars: int
    spike_body_min_pct: int
    spike_close_divisor: int
    spike_min_move_atr: Fraction
    quality_body_min_pct: int
    quality_max_range_atr: Fraction
    max_label: int
    order_type: str
    ttl_bars: int
    trigger_offset: int
    limit_offset: int
    stop_offset: int
    adr_fraction: Fraction
    fallback_risk_multiple: int
    late_session_2r: bool
    tail_window_minutes: int
    max_stops_per_day: int
    microchannel_min_bars: int
    range_position_divisor: int
    exhaustion_run_min: int
    exhaustion_range_atr: Fraction
    exhaustion_lookback: int
    groups: Mapping[str, Mapping[str, int]]
    instruments: Mapping[str, str]
    setups: tuple[Mapping[str, Any], ...]

    def group(self, root: str) -> Mapping[str, int]:
        try:
            return self.groups[self.instruments[root]]
        except KeyError:
            raise StrategyConfigError(f"PA 配置里没有品种 {root}") from None

    def setup(self, name: str) -> Mapping[str, Any]:
        return next(s for s in self.setups if s["name"] == name)

    @property
    def enabled_setups(self) -> tuple[str, ...]:
        return tuple(s["name"] for s in self.setups if s["enabled"])


def _need(d: Mapping, path: str, keys: set[str]):
    if not isinstance(d, Mapping):
        raise StrategyConfigError(f"{path} 应为映射")
    extra, missing = set(d) - keys, keys - set(d)
    if extra:
        raise StrategyConfigError(f"{path} 有未知的键：{sorted(extra)}")
    if missing:
        raise StrategyConfigError(f"{path} 缺少键：{sorted(missing)}")


def _int(v, path, lo=None, hi=None) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or (lo is not None and v < lo) or (hi is not None and v > hi):
        raise StrategyConfigError(f"{path} 应为{'' if lo is None else f' ≥{lo}'}{'' if hi is None else f' ≤{hi}'} 的整数：{v!r}")
    return v


def _frac(v, path) -> Fraction:
    try:
        f = Fraction(str(v))
    except (ValueError, ZeroDivisionError):
        raise StrategyConfigError(f"{path} 应为正数：{v!r}") from None
    if isinstance(v, bool) or f <= 0:
        raise StrategyConfigError(f"{path} 应为正数：{v!r}")
    return f


def parse_config(raw: Mapping[str, Any]) -> PaConfig:
    """校验并解析配置。未知键、非法阈值、启用未实现的形态都会报错。"""
    _need(raw, "配置", {"strategy_family", "version", "timeframe", "params", "groups", "instruments", "setups"})
    if raw["strategy_family"] != FAMILY:
        raise StrategyConfigError(f"strategy_family 应为 {FAMILY}：{raw['strategy_family']!r}")
    p = raw["params"]
    _need(p, "params", {"spike", "quality", "pullback", "entry", "target", "discipline", "microchannel_min_bars",
                        "range_position_divisor", "exhaustion"})
    _need(p["spike"], "params.spike", {"min_bars", "body_min_pct", "close_extreme_divisor", "min_move_atr"})
    _need(p["quality"], "params.quality", {"body_min_pct", "max_range_atr"})
    _need(p["pullback"], "params.pullback", {"max_label"})
    _need(p["entry"], "params.entry", {"order_type", "ttl_bars", "trigger_offset_ticks", "limit_offset_ticks",
                                       "stop_offset_ticks"})
    _need(p["target"], "params.target", {"adr_fraction", "fallback_risk_multiple", "late_session_2r"})
    _need(p["discipline"], "params.discipline", {"tail_window_minutes", "max_stops_per_day"})
    _need(p["exhaustion"], "params.exhaustion", {"ema_side_run_min", "range_atr", "lookback"})
    if p["entry"]["order_type"] != "STOP_LIMIT_NEXT_BAR":
        raise StrategyConfigError("params.entry.order_type 在 v1.0 只支持 STOP_LIMIT_NEXT_BAR")
    if not isinstance(p["target"]["late_session_2r"], bool):
        raise StrategyConfigError("params.target.late_session_2r 应为布尔值")
    if p["target"]["late_session_2r"]:
        raise StrategyConfigError("params.target.late_session_2r 在 v1.0 尚未实现，不能启用")

    groups = {}
    if not isinstance(raw["groups"], Mapping) or not raw["groups"]:
        raise StrategyConfigError("groups 不能为空")
    for name, g in raw["groups"].items():
        _need(g, f"groups.{name}", {"max_retrace_pct", "max_pullback_bars"})
        groups[name] = {"max_retrace_pct": _int(g["max_retrace_pct"], f"groups.{name}.max_retrace_pct", 1, 100),
                        "max_pullback_bars": _int(g["max_pullback_bars"], f"groups.{name}.max_pullback_bars", 1)}
    for root, g in raw["instruments"].items():
        if g not in groups:
            raise StrategyConfigError(f"instruments.{root} 引用了未定义的组 {g!r}")

    setups, seen = [], set()
    if not isinstance(raw["setups"], list):
        raise StrategyConfigError("setups 应为列表")
    for s in raw["setups"]:
        _need(s, "setups 的条目", {"name", "enabled", "exempt"})
        name = s["name"]
        if name not in KNOWN_SETUPS:
            raise StrategyConfigError(f"未知的形态：{name!r}")
        if name in seen:
            raise StrategyConfigError(f"形态重复：{name}")
        seen.add(name)
        if not isinstance(s["enabled"], bool):
            raise StrategyConfigError(f"setups.{name}.enabled 应为布尔值")
        if s["enabled"] and name not in IMPLEMENTED_SETUPS:
            raise StrategyConfigError(f"形态 {name} 尚未实现，不能启用（量化规则需先经确认，见 docs/strategy-spec.md）")
        bad = [f for f in s["exempt"] if f not in FILTERS]
        if bad:
            raise StrategyConfigError(f"setups.{name}.exempt 有未知的过滤器：{bad}")
        if "counter_spike" in s["exempt"]:
            raise StrategyConfigError(f"setups.{name}：尖峰内禁止逆势（counter_spike）不可豁免")
        setups.append({"name": name, "enabled": s["enabled"], "exempt": tuple(s["exempt"])})
    if seen != set(KNOWN_SETUPS):
        raise StrategyConfigError(f"setups 缺少形态：{sorted(set(KNOWN_SETUPS) - seen)}")

    return PaConfig(
        version=str(raw["version"]), timeframe=str(raw["timeframe"]),
        spike_min_bars=_int(p["spike"]["min_bars"], "params.spike.min_bars", 2),
        spike_body_min_pct=_int(p["spike"]["body_min_pct"], "params.spike.body_min_pct", 1, 100),
        spike_close_divisor=_int(p["spike"]["close_extreme_divisor"], "params.spike.close_extreme_divisor", 2),
        spike_min_move_atr=_frac(p["spike"]["min_move_atr"], "params.spike.min_move_atr"),
        quality_body_min_pct=_int(p["quality"]["body_min_pct"], "params.quality.body_min_pct", 1, 100),
        quality_max_range_atr=_frac(p["quality"]["max_range_atr"], "params.quality.max_range_atr"),
        max_label=_int(p["pullback"]["max_label"], "params.pullback.max_label", 1, 9),
        order_type=p["entry"]["order_type"], ttl_bars=_int(p["entry"]["ttl_bars"], "params.entry.ttl_bars", 1),
        trigger_offset=_int(p["entry"]["trigger_offset_ticks"], "params.entry.trigger_offset_ticks", 1),
        limit_offset=_int(p["entry"]["limit_offset_ticks"], "params.entry.limit_offset_ticks", 0),
        stop_offset=_int(p["entry"]["stop_offset_ticks"], "params.entry.stop_offset_ticks", 1),
        adr_fraction=_frac(p["target"]["adr_fraction"], "params.target.adr_fraction"),
        fallback_risk_multiple=_int(p["target"]["fallback_risk_multiple"], "params.target.fallback_risk_multiple", 1),
        late_session_2r=False,
        tail_window_minutes=_int(p["discipline"]["tail_window_minutes"], "params.discipline.tail_window_minutes", 0),
        max_stops_per_day=_int(p["discipline"]["max_stops_per_day"], "params.discipline.max_stops_per_day", 1),
        microchannel_min_bars=_int(p["microchannel_min_bars"], "params.microchannel_min_bars", 2),
        range_position_divisor=_int(p["range_position_divisor"], "params.range_position_divisor", 2),
        exhaustion_run_min=_int(p["exhaustion"]["ema_side_run_min"], "params.exhaustion.ema_side_run_min", 1),
        exhaustion_range_atr=_frac(p["exhaustion"]["range_atr"], "params.exhaustion.range_atr"),
        exhaustion_lookback=_int(p["exhaustion"]["lookback"], "params.exhaustion.lookback", 1),
        groups=groups, instruments=dict(raw["instruments"]), setups=tuple(setups))


def load_config(path: Path | str = DEFAULT_CONFIG) -> PaConfig:
    """读取 YAML 配置文件。这是本模块唯一的文件读取，只在构造策略时由调用方触发；on_bar 不做 I/O。"""
    import yaml
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f))


# ---------------------------------------------------------------- 定向坐标与基本判定
def _o(bar: Bar, d: int) -> tuple[int, int, int, int]:
    """定向后的 (open, high, low, close)。d=-1 时价格取负并交换高低。"""
    return (bar.open, bar.high, bar.low, bar.close) if d > 0 else (-bar.open, -bar.low, -bar.high, -bar.close)


def _ge_atr(ticks: int, mult: Fraction, atr: int) -> bool:
    """ticks ≥ mult × ATR（ATR 为定点）。"""
    return ticks * FIXED_SCALE * mult.denominator >= mult.numerator * atr


def _gt_atr(ticks: int, mult: Fraction, atr: int) -> bool:
    return ticks * FIXED_SCALE * mult.denominator > mult.numerator * atr


def is_strong_trend_bar(bar: Bar, prev: Bar | None, d: int, cfg: PaConfig) -> bool:
    """强趋势 K：顺向收盘、实体 ≥ 全幅的 body_min_pct%、收在方向一侧的 1/N 之内、创出新的方向极值。"""
    if prev is None:
        return False
    o, h, l, c = _o(bar, d)
    rng = h - l
    if rng <= 0 or c <= o:
        return False
    return ((c - o) * 100 >= cfg.spike_body_min_pct * rng and (h - c) * cfg.spike_close_divisor <= rng
            and h > _o(prev, d)[1])


def signal_bar_quality(bar: Bar, atr: int, cfg: PaConfig) -> tuple[bool, str | None, dict]:
    """通用质量检查：全幅不为零、实体 ≥ 全幅的 body_min_pct%、全幅 ≤ max_range_atr × ATR。"""
    rng = bar.high - bar.low
    body = abs(bar.close - bar.open)
    ev = {"range_ticks": rng, "body_ticks": body, "atr14_fixed": atr}
    if rng <= 0:
        return False, "QUALITY_ZERO_RANGE", ev
    if body * 100 < cfg.quality_body_min_pct * rng:
        return False, "QUALITY_BODY_TOO_SMALL", ev
    if _gt_atr(rng, cfg.quality_max_range_atr, atr):
        return False, "QUALITY_RANGE_TOO_LARGE", ev
    return True, None, ev


def _is_signal_shape(bar: Bar, d: int) -> bool:
    """顺势信号 K 的形状（Spike 回调）：顺向收盘，且收在全幅的顺向半部。"""
    o, h, l, c = _o(bar, d)
    return c > o and 2 * c >= h + l


# ---------------------------------------------------------------- 过滤器
def evaluate_filters(cand: Mapping[str, Any], ctx: FeatureContext, spike: Mapping | None, htf_direction: str,
                     cfg: PaConfig) -> tuple[str | None, list[dict]]:
    """按固定顺序评估五个过滤器。返回 (第一个拦截原因或 None, 每个过滤器的结论)。

    cand：{"setup", "d"（+1 做多 / -1 做空）, "label", "bar"}。
    """
    exempt = set(cfg.setup(cand["setup"])["exempt"])
    d, bar = cand["d"], cand["bar"]
    results = []

    def add(name, triggered, reason, **ev):
        results.append({"filter": name, "exempt": name in exempt, "triggered": bool(triggered),
                        "reason": reason if triggered else None, **ev})

    # 1) H1/L1 拦截
    add("h1l1_block", cand.get("label") in ("H1", "L1"), "H1L1_BLOCKED", label=cand.get("label"))
    # 2) 尖峰内禁止逆势
    add("counter_spike", bool(spike) and spike["d"] != d, "COUNTER_SPIKE",
        spike_direction=None if not spike else ("BULL" if spike["d"] > 0 else "BEAR"))
    # 3) 逆 1 小时方向。方向未知时，需要该条件的形态一律等待。
    want = "BULL" if d > 0 else "BEAR"
    if htf_direction == "UNKNOWN":
        add("counter_htf", True, "HTF_DIRECTION_UNKNOWN", htf_direction=htf_direction)
    else:
        add("counter_htf", htf_direction != want, "COUNTER_HTF", htf_direction=htf_direction)
    # 4) 5 分钟区间低买高卖：做多的信号 K 低点须在近 20 根区间的下 1/N，做空镜像
    r = ctx.rolling_20_range
    if r is None:
        add("range_position", True, "FEATURE_UNAVAILABLE:rolling_20_range")
    else:
        width = r.high - r.low
        dist = (bar.low - r.low) if d > 0 else (r.high - bar.high)
        add("range_position", dist * cfg.range_position_divisor > width, "RANGE_POSITION",
            range_high=r.high, range_low=r.low, distance_ticks=dist)
    # 5) 衰竭：连续 ≥N 根收在 EMA 顺向一侧，且信号 K 全幅 ≥ k×ATR 或为近 M 根最大
    run = ctx.ema_side_run * d
    rng = bar.high - bar.low
    recent = [b.high - b.low for b in ctx.bars[-cfg.exhaustion_lookback:]]
    big = (ctx.atr14 is not None and _ge_atr(rng, cfg.exhaustion_range_atr, ctx.atr14)) or rng >= max(recent)
    add("exhaustion", run >= cfg.exhaustion_run_min and big, "EXHAUSTION", ema_side_run=ctx.ema_side_run,
        range_ticks=rng)

    first = next((x["reason"] for x in results if x["triggered"] and not x["exempt"]), None)
    return first, results


# ---------------------------------------------------------------- 策略
def _target(ctx: FeatureContext, bar: Bar, cfg: PaConfig) -> tuple[TargetSpec, str, dict]:
    adr = ctx.adr10
    if bar.session_type == "RTH" and adr is not None:
        v = adr.value_ticks * cfg.adr_fraction
        ticks = max(1, -(-v.numerator // v.denominator))          # 向上取整，至少 1 tick
        return (TargetSpec("OFFSET_FROM_FILL_TICKS", ticks), "ADR_FRACTION",
                {"adr_ticks": str(adr.value_ticks), "adr_valid_days": adr.valid_days,
                 "adr_fraction": str(cfg.adr_fraction)})
    return (TargetSpec("INITIAL_RISK_MULTIPLE", cfg.fallback_risk_multiple), "RISK_MULTIPLE_FALLBACK",
            {"adr_valid_days": adr.valid_days if adr else 0,
             "why": "ADR 不可用" if adr is None else "非 RTH"})


def make_setup_id(family: str, contract_id: str, setup: str, signal_bar_end_utc: int, version: str) -> str:
    raw = f"{family}|{contract_id}|{setup}|{signal_bar_end_utc}|{version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _bucket(n: int) -> str:
    return "1-3" if n <= 3 else "4-9" if n <= 9 else "10+"


class PaBrooksStrategy:
    strategy_family = FAMILY

    def __init__(self, config: PaConfig):
        self.config = config
        self.version = config.version

    @classmethod
    def from_default_config(cls) -> "PaBrooksStrategy":
        return cls(load_config())

    def feature_requirements(self) -> FeatureRequirements:
        return FeatureRequirements(frozenset({"ema20", "atr14", "ema_side_run", "adr10", "htf_1h", "swing_pivots",
                                              "rolling_20_range", "intraday_rth_range", "reference_levels"}),
                                   warmup_bars=20)

    def initial_state(self) -> StrategyState:
        return StrategyState(STATE_SCHEMA_VERSION, {
            "day": None, "stops": 0, "halted": False, "seen_trades": 0,
            "run": None, "spike": None, "micro": [0, 0], "last_signal": None})

    # ------------------------------------------------------------ 逐 bar 决策
    def on_bar(self, ctx: FeatureContext, prev_state: StrategyState) -> StrategyStepResult:
        cfg = self.config
        if prev_state.state_schema_version != STATE_SCHEMA_VERSION:
            raise ContractViolation(f"不支持的状态结构版本：{prev_state.state_schema_version}")
        s = prev_state.data
        bar, prev = ctx.bar, ctx.prev_bar
        grp = cfg.group(ctx.instrument_root)
        ended = None
        skip = None
        cand = None
        filt: list[dict] = []
        quality: dict = {}

        # 交易日切换：结构状态和止损计数都重新开始
        if s["day"] != bar.trading_day:
            s = self.initial_state().data
            s["day"] = bar.trading_day

        # 两次止损停手：只看本交易日新完结的纸面交易
        for t in ctx.closed_trades[s["seen_trades"]:]:
            if s["halted"]:
                break
            if t.exit_reason == "STOP":
                s["stops"] += 1
                if s["stops"] >= cfg.max_stops_per_day:
                    s["halted"] = True
            elif t.net_sign > 0:
                s["stops"] = 0                      # 只有净盈利的已完结交易能清零
        s["seen_trades"] = len(ctx.closed_trades)

        if bar.session_type != "RTH":
            return self._result(ctx, s, "WAIT", skip="OUTSIDE_SCAN_SESSION")
        atr = ctx.atr14
        if atr is None:
            s["run"], s["spike"], s["last_signal"] = None, None, None
            return self._result(ctx, s, "WAIT", skip="FEATURE_UNAVAILABLE:atr14")

        # 微通道计数：连续不破前一根低点（牛）/ 高点（熊）
        if prev is not None:
            s["micro"] = [s["micro"][0] + 1 if bar.low >= prev.low else 0,
                          s["micro"][1] + 1 if bar.high <= prev.high else 0]

        # 1) 强趋势 K 的连续段，以及 Spike 的确认或延伸
        confirmed_now = False
        d_strong = next((d for d in (1, -1) if is_strong_trend_bar(bar, prev, d, cfg)), 0)
        run = s["run"]
        if d_strong:
            o, h, l, c = _o(bar, d_strong)
            if run and run["d"] == d_strong:
                run = {**run, "count": run["count"] + 1}
            else:
                run = {"d": d_strong, "first_open": o, "start": bar.start_utc, "count": 1}
            move = c - run["first_open"]
            if run["count"] >= cfg.spike_min_bars and _ge_atr(move, cfg.spike_min_move_atr, atr):
                confirmed_now = True
                s["spike"] = {"d": d_strong, "first_open": run["first_open"], "start": run["start"],
                              "end": bar.end_utc, "extreme": h, "move": move, "atr": atr, "bars": run["count"],
                              "phase": "SPIKE", "pb_bars": 0, "pb_extreme": None,
                              "leg_open": False, "leg_first": None, "leg_used": False, "labels": 0}
        else:
            run = None
        s["run"] = run

        # 2) 已有 Spike：延伸、回调、失效、恢复、H/L 计数
        sp = s["spike"]
        if sp and not confirmed_now:
            d = sp["d"]
            o, h, l, c = _o(bar, d)
            pl = _o(prev, d)[2] if prev is not None else l
            if sp["phase"] == "SPIKE" and h > sp["extreme"]:
                sp["extreme"] = h                    # 不够强但仍创新极值：Spike 延伸
                sp["end"] = bar.end_utc
            else:
                if sp["phase"] == "SPIKE":
                    sp["phase"], sp["pb_bars"], sp["pb_extreme"] = "PULLBACK", 1, l
                else:
                    sp["pb_bars"] += 1
                    sp["pb_extreme"] = min(sp["pb_extreme"], l)
                retr = sp["extreme"] - sp["pb_extreme"]
                denom = sp["extreme"] - sp["first_open"]
                if retr * 100 > grp["max_retrace_pct"] * denom:
                    ended = "INVALIDATED_RETRACE"
                elif sp["pb_bars"] > grp["max_pullback_bars"]:
                    ended = "INVALIDATED_BARS"
                elif c > sp["extreme"]:
                    ended = "TREND_RESUMED"
                else:
                    if l < pl:                        # 新的反向腿：严格破前一根的低点（定向后）
                        if not sp["leg_open"] or sp["leg_used"]:
                            sp["leg_open"], sp["leg_first"], sp["leg_used"] = True, bar.start_utc, False
                    if (sp["leg_open"] and not sp["leg_used"] and bar.start_utc > sp["leg_first"]
                            and sp["labels"] < cfg.max_label and _is_signal_shape(bar, d)):
                        ok, why, quality = signal_bar_quality(bar, atr, cfg)
                        if not ok:
                            skip = why
                        else:
                            sp["labels"] += 1
                            sp["leg_used"] = True
                            cand = {"setup": "spike_pullback", "d": d, "bar": bar,
                                    "label": f"{'H' if d > 0 else 'L'}{sp['labels']}"}
            if ended:
                snapshot = dict(sp)
                s["spike"] = None
                s["last_signal"] = None
                return self._result(ctx, s, "WAIT", ended=ended, ended_spike=snapshot)

        s["last_signal"] = None
        if cand is None:
            return self._result(ctx, s, "WAIT", skip=skip, quality=quality)

        # 3) 过滤器链与纪律
        sp = s["spike"]
        skip, filt = evaluate_filters(cand, ctx, sp, "UNKNOWN", cfg)
        if skip is None and bar.end_utc > ctx.rth_close_utc - cfg.tail_window_minutes * 60:
            skip = "TAIL_WINDOW_UNCONFIGURED"
        if skip is None and s["halted"]:
            skip = "TWO_STOPS_HALT"
        if skip is not None:
            return self._result(ctx, s, "WAIT", skip=skip, cand=cand, filt=filt, quality=quality)

        # 4) 订单意图
        d = cand["d"]
        o, h, l, c = _o(bar, d)
        trigger = d * (h + cfg.trigger_offset)
        limit = d * (h + cfg.trigger_offset + cfg.limit_offset)
        stop = d * (sp["pb_extreme"] - cfg.stop_offset)
        target, rule, tev = _target(ctx, bar, cfg)
        setup_id = make_setup_id(FAMILY, ctx.contract_id, f"{cand['setup']}:{cand['label']}", bar.end_utc, self.version)
        intent = OrderIntent(
            side="LONG" if d > 0 else "SHORT", order_type=cfg.order_type, trigger_ticks=trigger, limit_ticks=limit,
            ttl_bars=cfg.ttl_bars, stop_ticks=stop, target=target, setup_id=setup_id,
            evidence={"setup": cand["setup"], "label": cand["label"], "signal_bar_start_utc": bar.start_utc,
                      "signal_bar_end_utc": bar.end_utc, "pullback_extreme_ticks": d * sp["pb_extreme"],
                      "target_rule": rule, **tev})
        s["last_signal"] = {"setup_id": setup_id, "d": d, "trigger": trigger, "limit": limit, "stop": stop,
                            "label": cand["label"], "valid_until": bar.end_utc + (bar.end_utc - bar.start_utc) * cfg.ttl_bars}
        return self._result(ctx, s, "SETUP_VALID", cand=cand, filt=filt, quality=quality, intent=intent,
                            target_rule=rule, target_ev=tev)

    # ------------------------------------------------------------ 结果组装
    def _cycle(self, s: dict) -> str:
        sp = s.get("spike")
        if sp:
            return "BULL_SPIKE" if sp["d"] > 0 else "BEAR_SPIKE"
        n = self.config.microchannel_min_bars
        bull, bear = s["micro"][0] >= n, s["micro"][1] >= n
        if bull != bear:
            return "BULL_MICROCHANNEL" if bull else "BEAR_MICROCHANNEL"
        return "UNKNOWN"

    def _result(self, ctx: FeatureContext, s: dict, status: str, *, skip=None, cand=None, filt=(), quality=None,
                intent: OrderIntent | None = None, ended=None, ended_spike=None, target_rule=None,
                target_ev=None) -> StrategyStepResult:
        bar = ctx.bar
        sp = s.get("spike")
        view = sp or ended_spike
        d = view["d"] if view else 0
        structure = {
            "cycle": self._cycle(s), "htf_direction": "UNKNOWN",
            "htf_note": "1 小时方向的量化规则未获批，v1.0 恒为 UNKNOWN",
            "spike": None if not view else {
                "direction": "BULL" if d > 0 else "BEAR", "phase": "ENDED" if ended else view["phase"],
                "start_utc": view["start"], "end_utc": view["end"], "bars": view["bars"],
                "first_open_ticks": d * view["first_open"], "extreme_ticks": d * view["extreme"],
                "move_ticks": view["move"], "atr14_fixed": view["atr"],
                "pullback_bars": view["pb_bars"],
                "pullback_extreme_ticks": None if view["pb_extreme"] is None else d * view["pb_extreme"],
                "pullback_bucket": _bucket(view["pb_bars"]) if view["pb_bars"] else None,
                "labels_used": view["labels"], "ended": ended},
            "stops_today": s["stops"], "halted": s["halted"],
            "enabled_setups": list(self.config.enabled_setups),
        }
        if status == "SETUP_VALID":
            nxt = "下一根 5 分钟线内触及触发价则挂单生效；未触发则到期撤单"
        elif skip == "FEATURE_UNAVAILABLE:atr14":
            nxt = "等待 ATR14 预热完成（需要 14 根已收线的 K）"
        elif skip == "OUTSIDE_SCAN_SESSION":
            nxt = "等待 RTH 开盘"
        elif sp and sp["phase"] == "SPIKE":
            nxt = "等待 Spike 之后第一根不再创新极值的 K（回调第 1 根）"
        elif sp:
            nxt = ("等待新的反向腿之后出现合格的顺势信号 K" if (not sp["leg_open"] or sp["leg_used"])
                   else "等待反向腿之后第一根合格的顺势信号 K")
        else:
            nxt = "等待至少 2 根连续强趋势 K 且幅度 ≥ 2.5×ATR 的 Spike"
        meta = DecisionMetadata(
            strategy_family=FAMILY, strategy_version=self.version, contract_id=ctx.contract_id,
            signal_bar_end_utc=bar.end_utc, known_at_utc=ctx.known_at_utc, signal_status=status,
            side=None if cand is None else ("LONG" if cand["d"] > 0 else "SHORT"),
            setup=None if cand is None else cand["setup"], setup_id=None if intent is None else intent.setup_id,
            label=None if cand is None else cand["label"], skip_reason=skip,
            valid_until_utc=None if intent is None else bar.end_utc + (bar.end_utc - bar.start_utc) * intent.ttl_bars,
            entry_trigger_ticks=None if intent is None else intent.trigger_ticks,
            entry_limit_ticks=None if intent is None else intent.limit_ticks,
            stop_ticks=None if intent is None else intent.stop_ticks,
            target=None if intent is None else intent.target.to_dict(), target_rule=target_rule,
            next_confirmation=nxt, structure=structure,
            evidence={"signal_bar": {"start_utc": bar.start_utc, "end_utc": bar.end_utc, "open": bar.open,
                                     "high": bar.high, "low": bar.low, "close": bar.close},
                      "signal_bar_quality": dict(quality or {}), "filters": list(filt),
                      "target": dict(target_ev or {}), "atr14_fixed": ctx.atr14,
                      "reference_levels": dict(ctx.reference_levels)})
        return StrategyStepResult(status, intent, meta, StrategyState(STATE_SCHEMA_VERSION, s))

    # ------------------------------------------------------------ 人工计划一致性
    def evaluate_pretrade_alignment(self, ctx: FeatureContext, state: StrategyState,
                                    plan: TraderPlan) -> AlignmentResult:
        s = state.data
        d = 1 if plan.side == "LONG" else -1
        flags = [] if plan.stop_ticks is not None else ["NO_STOP"]
        sp, sig = s.get("spike"), s.get("last_signal")
        ev: dict[str, Any] = {"cycle": self._cycle(s)}
        if sp and sp["d"] != d:
            ev["spike_direction"] = "BULL" if sp["d"] > 0 else "BEAR"
            ev["spike_phase"] = sp["phase"]
            return AlignmentResult("DEVIATES", (*flags, "COUNTERTREND_BEFORE_CONFIRMATION"), None, ev)
        if sig and sig["d"] == d and ctx.as_of_utc <= sig["valid_until"]:
            ev.update(setup_trigger_ticks=sig["trigger"], setup_stop_ticks=sig["stop"], label=sig["label"])
            if plan.stop_ticks is not None and d * (sig["stop"] - plan.stop_ticks) > 0:
                flags.append("STOP_WIDER_THAN_SETUP")
            if d * (plan.entry_ticks - sig["limit"]) > 0:
                flags.append("ENTRY_BEYOND_SETUP_LIMIT")
            return AlignmentResult("ALIGNED", tuple(flags), sig["setup_id"], ev)
        if sp:
            flags.append("NO_ACTIVE_SETUP")
        return AlignmentResult("UNKNOWN", tuple(flags), None, ev)


# ---------------------------------------------------------------- 中文短报
def render_zh(result: StrategyStepResult, tick_size) -> str:
    """把决策渲染为中文：先给新信号及其状态，再给周期、最新 K 线、参考价和下一确认条件。"""
    from decimal import Decimal

    from ..sessions import et       # 纯时区换算，无 I/O

    tick = Decimal(str(tick_size))
    px = lambda t: "—" if t is None else str(t * tick)        # noqa: E731
    m = result.decision_metadata
    st, sb = m.structure, m.evidence["signal_bar"]
    when = et(m.signal_bar_end_utc).strftime("%Y-%m-%d %H:%M ET")
    lines = []
    if result.signal_status == "SETUP_VALID":
        side = "做多" if m.side == "LONG" else "做空"
        tgt = m.target
        tgt_text = (f"成交价{'+' if m.side == 'LONG' else '-'}{Decimal(tgt['value']) * tick} 点（0.40×ADR）"
                    if tgt["kind"] == "OFFSET_FROM_FILL_TICKS" else f"{tgt['value']}R（ADR 不可用）"
                    if tgt["kind"] == "INITIAL_RISK_MULTIPLE" else px(tgt["value"]))
        lines.append(f"新信号：{side} Spike 回调 {m.label}，SETUP_VALID。下一根止损限价单：触发 {px(m.entry_trigger_ticks)}，"
                     f"限价 {px(m.entry_limit_ticks)}，止损 {px(m.stop_ticks)}，目标 {tgt_text}。"
                     f"有效至 {et(m.valid_until_utc).strftime('%H:%M ET')}。setup_id {m.setup_id}。")
    elif m.setup:
        lines.append(f"新信号：{'做多' if m.side == 'LONG' else '做空'} Spike 回调 {m.label} 被拦截，WAIT。原因：{m.skip_reason}。")
    elif m.skip_reason:
        lines.append(f"新信号：无，{result.signal_status}。原因：{m.skip_reason}。")
    else:
        lines.append(f"新信号：无，{result.signal_status}。")
    sp = st["spike"]
    cyc = st["cycle"]
    if sp:
        cyc += (f"（{'牛' if sp['direction'] == 'BULL' else '熊'} Spike {sp['bars']} 根，极值 {px(sp['extreme_ticks'])}"
                + (f"，回调第 {sp['pullback_bars']} 根，回调极值 {px(sp['pullback_extreme_ticks'])}" if sp["pullback_bars"] else "")
                + (f"，已结束：{sp['ended']}" if sp["ended"] else "") + "）")
    lines.append(f"周期：{cyc}；1 小时方向 {st['htf_direction']}。")
    lines.append(f"最新 K 线（{when} 收）：开 {px(sb['open'])} 高 {px(sb['high'])} 低 {px(sb['low'])} 收 {px(sb['close'])}。")
    lv = m.evidence["reference_levels"]
    if lv:
        lines.append("参考价：" + "，".join(f"{k.upper()} {px(v) if isinstance(v, int) else '不可用'}"
                                          for k, v in lv.items() if k in ("pdh", "pdl", "prev_rth_close", "onh", "onl")) + "。")
    if st["halted"]:
        lines.append(f"纪律：今日已连续止损 {st['stops_today']} 次，停手。")
    lines.append(f"下一确认条件：{m.next_confirmation}。")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
