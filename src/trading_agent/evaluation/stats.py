"""评测用的统计：配对日差、连续交易日区块 bootstrap 的单侧置信界、回撤、差异交易。只做算术，不读任何数据。"""
from __future__ import annotations

import numpy as np

from ..research.power import block_indices


def max_drawdown(xs) -> float:
    cum = peak = dd = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def paired(base: list[float], cand: list[float]) -> list[float]:
    if len(base) != len(cand):
        raise ValueError("两臂的交易日数不一致")
    return [c - b for b, c in zip(base, cand)]


def block_bound(values, *, alpha: float, seed: int, block: int, resamples: int, min_tail: int, max_resamples: int,
                side: str = "lower") -> dict:
    """均值的单侧置信界（循环区块 bootstrap 的分位数）。估不可靠时 bound 为空并给出原因。"""
    x = np.asarray(values, dtype=float)
    n = len(x)
    out = {"n": n, "mean": float(x.mean()) if n else None, "alpha": alpha, "block": block, "side": side,
           "bound": None, "resamples": 0, "reason": None}
    if n < 2 * block:
        out["reason"] = "TOO_FEW_DAYS_FOR_BLOCKS"
        return out
    b = resamples
    while alpha * b < min_tail and b < max_resamples:
        b = min(b * 2, max_resamples)
    if alpha * b < min_tail:
        out.update(resamples=b, reason="TAIL_TOO_SPARSE")
        return out
    rng = np.random.default_rng(seed)
    means = x[block_indices(rng, n, n, block, b)].mean(axis=1)
    q = alpha if side == "lower" else 1 - alpha
    out.update(resamples=b, bound=float(np.quantile(means, q)))
    return out


def delta_trades(base_trades: list[dict], cand_trades: list[dict], unit: float, *, alpha: float, seed: int,
                 resamples: int) -> dict:
    """基线入场但候选拦截的交易：平均净 R 及其单侧置信上界（逐笔重采样）。只作辅助。"""
    cand_ids = {t["setup_id"] for t in cand_trades}
    base_ids = {t["setup_id"] for t in base_trades}
    blocked = [float(t["net_usd"]) / unit for t in base_trades if t["setup_id"] not in cand_ids]
    out = {"blocked_trades": len(blocked), "candidate_only_trades": sum(t["setup_id"] not in base_ids for t in cand_trades),
           "blocked_mean_r": (sum(blocked) / len(blocked)) if blocked else None, "upper_bound": None}
    if len(blocked) >= 10:
        rng = np.random.default_rng(seed)
        arr = np.asarray(blocked)
        means = arr[rng.integers(0, len(arr), size=(resamples, len(arr)))].mean(axis=1)
        out["upper_bound"] = float(np.quantile(means, 1 - alpha))
    return out
