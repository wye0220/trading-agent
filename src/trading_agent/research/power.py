"""功效测算：40 日确认到底能分辨多大的效应。

输入是开发段逐日的基线与扰动的净 R 和成交数。这里的统计量是浮点，只用于规划，不参与信号、成交或盈亏。
合成扰动只用来估计配对日差的波动和连续日依赖，不是任何候选盈利的证据。
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

from .config import PowerConfig

ELIGIBLE, INSUFFICIENT = "ELIGIBLE", "EVIDENCE_INSUFFICIENT"
_Z = NormalDist()


# ---------------------------------------------------------------- 描述统计与正态近似
def autocorr1(x) -> float | None:
    x = np.asarray(x, dtype=float)
    if len(x) < 3:
        return None
    a, b = x[:-1] - x.mean(), x[1:] - x.mean()
    den = float(((x - x.mean()) ** 2).sum())
    return None if den == 0 else float((a * b).sum() / den)


def describe(net, trades) -> dict:
    net, trades = np.asarray(net, dtype=float), np.asarray(trades, dtype=int)
    n = len(net)
    return {"days": n, "trades": int(trades.sum()), "trades_per_day": float(trades.mean()) if n else None,
            "zero_trade_day_share": float((trades == 0).mean()) if n else None,
            "net_per_day_mean": float(net.mean()) if n else None,
            "net_per_day_std": float(net.std(ddof=1)) if n > 1 else None,
            "autocorr_lag1": autocorr1(net)}


def required_days(sigma: float, delta: float, alpha: float, power: float) -> float | None:
    """独立正态近似：检出每日 δ 所需的确认日数。只用于看量级。"""
    if not sigma or not delta or delta <= 0:
        return None
    return ((_Z.inv_cdf(1 - alpha) + _Z.inv_cdf(power)) * sigma / delta) ** 2


def detectable_effect(sigma: float, days: float, alpha: float, power: float) -> float | None:
    """独立正态近似：给定日数下可检出的最小每日效应。"""
    if not sigma or not days or days <= 0:
        return None
    return (_Z.inv_cdf(1 - alpha) + _Z.inv_cdf(power)) * sigma / math.sqrt(days)


# ---------------------------------------------------------------- 区块 bootstrap
def block_indices(rng: np.random.Generator, n_obs: int, length: int, block: int, size: int) -> np.ndarray:
    """循环区块 bootstrap 的下标，形状 (size, length)。每个区块是连续的交易日，首尾相接。"""
    nblocks = math.ceil(length / block)
    starts = rng.integers(0, n_obs, size=(size, nblocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n_obs
    return idx.reshape(size, nblocks * block)[:, :length]


def _max_dd(x: np.ndarray) -> np.ndarray:
    cum = np.cumsum(x, axis=1)
    peak = np.maximum(np.maximum.accumulate(cum, axis=1), 0.0)
    return (peak - cum).max(axis=1).clip(min=0.0)


def simulate(base_net, base_trades, var_net, var_trades, track: str, cfg: PowerConfig, *, stress_r_per_trade: float,
             seed_offset: int = 0) -> dict:
    """对一个扰动模拟 40 日确认。

    每次模拟：从开发段循环抽取连续区块凑成一个确认窗；把配对差平移到设定的真实效应 δ
    （扰动的每日净 R 统一加上 δ 减去观察到的平均差）；再按该轨道的门槛判定。
    返回各 δ 下每个条件和联合条件的通过率。
    """
    b, v = np.asarray(base_net, dtype=float), np.asarray(var_net, dtype=float)
    nb, nv = np.asarray(base_trades, dtype=float), np.asarray(var_trades, dtype=float)
    n_obs, L = len(b), cfg.confirm_days
    rng = np.random.default_rng(cfg.seed + seed_offset)
    outer = block_indices(rng, n_obs, L, cfg.block_length_days, cfg.simulations)
    inner = block_indices(rng, L, L, cfg.block_length_days, cfg.inner_resamples)
    obs_mean = float((v - b).mean())
    B, V, NB, NV = b[outer], v[outer], nb[outer], nv[outer]
    D0 = (V - B) - obs_mean                           # 平移到真实效应为 0 的配对差
    # 内层：每个确认窗里配对差均值的单侧 α 置信下界。平移一个常数，下界跟着平移同一个常数，所以只算一次。
    lb0 = np.empty(cfg.simulations)
    for i in range(0, cfg.simulations, 200):
        chunk = D0[i:i + 200]
        means = chunk[:, inner].mean(axis=2)          # (块内模拟数, 内层次数)
        lb0[i:i + 200] = np.quantile(means, cfg.alpha, axis=1)
    mean0 = D0.mean(axis=1)
    trades_ok = (NB.sum(axis=1) >= cfg.min_trades_per_arm) & (NV.sum(axis=1) >= cfg.min_trades_per_arm)
    base_stress = B.sum(axis=1) - stress_r_per_trade * NB.sum(axis=1)
    dd_b = _max_dd(B)
    rows = []
    for delta in cfg.true_effect_grid:
        Vd = V + (delta - obs_mean)
        lb, mean = lb0 + delta, mean0 + delta
        dd_v = _max_dd(Vd)
        cand_stress = Vd.sum(axis=1) - stress_r_per_trade * NV.sum(axis=1)
        stress_ok = (cand_stress > 0) & (cand_stress >= base_stress)
        if track == "PRUNING_DEFENSIVE":
            ci_ok = lb > -cfg.margin_r_per_day
            dd_ok = dd_v <= dd_b * (1 - cfg.drawdown_improvement_min_pct / 100.0)
            effect_ok = np.ones_like(ci_ok)
        else:
            ci_ok = lb > 0
            dd_ok = (dd_v - dd_b) <= cfg.drawdown_increase_max_r
            effect_ok = mean >= cfg.delta_r_per_day
        joint = ci_ok & dd_ok & effect_ok & stress_ok & trades_ok
        rows.append({"true_effect": delta, "ci": float(ci_ok.mean()), "effect": float(effect_ok.mean()),
                     "drawdown": float(dd_ok.mean()), "stress": float(stress_ok.mean()),
                     "min_trades": float(trades_ok.mean()), "joint": float(joint.mean()),
                     "joint_without_drawdown": float((ci_ok & effect_ok & stress_ok & trades_ok).mean()),
                     "statistical_only": float((ci_ok & effect_ok).mean())})
    return {"track": track, "rows": rows, "observed_mean_diff": obs_mean,
            "min_effect_for_target": min_effect(rows, "joint", cfg.power_target),
            "min_effect_statistical_only": min_effect(rows, "statistical_only", cfg.power_target)}


def min_effect(rows: list[dict], key: str, target: float) -> float | None:
    """联合通过率第一次达到目标的真实效应（网格内线性插值）。网格内达不到返回 None。"""
    prev = None
    for r in rows:
        if r[key] >= target:
            if prev is None or r[key] == prev[key]:
                return r["true_effect"]
            w = (target - prev[key]) / (r[key] - prev[key])
            return prev["true_effect"] + w * (r["true_effect"] - prev["true_effect"])
        prev = r
    return None


def _at(rows: list[dict], effect: float, key: str = "joint") -> float | None:
    for r in rows:
        if abs(r["true_effect"] - effect) < 1e-12:
            return r[key]
    lo = [r for r in rows if r["true_effect"] <= effect]
    hi = [r for r in rows if r["true_effect"] >= effect]
    if not lo or not hi:
        return None
    a, c = lo[-1], hi[0]
    w = (effect - a["true_effect"]) / (c["true_effect"] - a["true_effect"])
    return a[key] + w * (c[key] - a[key])


# ---------------------------------------------------------------- 汇总
def analyze(base: dict, variants: list[dict], cfg: PowerConfig, *, stress_r_per_trade: float,
            available_days: int, fixture: bool) -> dict:
    """base、variants[i]["series"] 是 metrics.daily_series 的结果（同一组交易日）。"""
    base_desc = describe(base["net"], base["trades"])
    n_dev = base_desc["days"]
    out = {"alpha": cfg.alpha, "alpha_family": cfg.alpha_family, "max_formal_tests": cfg.max_formal_tests,
           "power_target": cfg.power_target, "confirm_days": cfg.confirm_days, "unit": base["unit"],
           "baseline": base_desc, "available_days": available_days, "variants": [], "flags": []}
    if fixture:
        out["flags"].append("FIXTURE_ONLY")
    if n_dev < cfg.min_dev_days:
        out["flags"].append("CI_UNRELIABLE")
    expected_trades = (base_desc["trades_per_day"] or 0) * cfg.confirm_days
    out["expected_trades_in_window"] = expected_trades
    for i, v in enumerate(variants):
        s = v["series"]
        diff = np.asarray(s["net"], dtype=float) - np.asarray(base["net"], dtype=float)
        sd = float(diff.std(ddof=1)) if len(diff) > 1 else None
        item = {"id": v["id"], "track": v["track"], "note": v.get("note"),
                "variant": describe(s["net"], s["trades"]),
                "paired": {"mean": float(diff.mean()) if len(diff) else None, "std": sd,
                           "autocorr_lag1": autocorr1(diff),
                           "days_changed": int((diff != 0).sum())},
                "normal_approx": {
                    "detectable_20d": detectable_effect(sd, 20, cfg.alpha, cfg.power_target),
                    "detectable_confirm": detectable_effect(sd, cfg.confirm_days, cfg.alpha, cfg.power_target),
                    "detectable_available": detectable_effect(sd, available_days, cfg.alpha, cfg.power_target),
                    "required_days_for_delta": required_days(sd, cfg.delta_r_per_day, cfg.alpha, cfg.power_target)},
                "simulation": None}
        if n_dev >= 2 * cfg.block_length_days and sd:
            item["simulation"] = simulate(base["net"], base["trades"], s["net"], s["trades"], v["track"], cfg,
                                          stress_r_per_trade=stress_r_per_trade, seed_offset=i + 1)
        out["variants"].append(item)

    out["tracks"] = {}
    for track, planning in (("PRUNING_DEFENSIVE", 0.0), ("ALPHA_EXPANSION", cfg.planning_true_effect)):
        items = [x for x in out["variants"] if x["track"] == track and x["simulation"]]
        rates = [_at(x["simulation"]["rows"], planning) for x in items]
        rates = [r for r in rates if r is not None]
        worst = min(rates) if rates else None
        ok = bool(rates) and worst >= cfg.power_target and not out["flags"]
        reasons = []
        if not items:
            reasons.append("没有可用的扰动结果")
        if fixture:
            reasons.append("合成数据（FIXTURE_ONLY）")
        if "CI_UNRELIABLE" in out["flags"]:
            reasons.append(f"开发段有效日 {n_dev} 少于 {cfg.min_dev_days}，区间估计不可靠")
        if rates and worst < cfg.power_target:
            reasons.append(f"规划效应下的整体通过率 {worst:.0%} 低于目标 {cfg.power_target:.0%}")
        out["tracks"][track] = {"planning_effect": planning, "joint_pass_rate": worst,
                                "eligibility": ELIGIBLE if ok else INSUFFICIENT, "reasons": reasons}
    sds = [x["paired"]["std"] for x in out["variants"] if x["paired"]["std"]]
    if sds:
        sd = max(sds)                                  # 保守：取波动最大的扰动
        need = required_days(sd, cfg.delta_r_per_day, cfg.alpha, cfg.power_target)
        out["availability"] = {
            "sigma_used": sd, "delta": cfg.delta_r_per_day, "required_days": need, "available_days": available_days,
            "sufficient": need is not None and available_days >= need,
            "min_testable_delta": detectable_effect(sd, available_days, cfg.alpha, cfg.power_target)}
    else:
        out["availability"] = None
    return out
