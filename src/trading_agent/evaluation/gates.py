"""评测门槛：盲筛的淘汰规则、正式确认的 Track A／B 门槛、标注类指标。输入是两臂的逐日序列，输出带数值的判定。"""
from __future__ import annotations

from . import stats
from .config import EvalConfig

CONTINUE, REJECTED, INSUFFICIENT = "SCREEN_CONTINUE", "SCREEN_REJECTED", "EVIDENCE_INSUFFICIENT"
PASS, FAIL = "PASS_FOR_SHADOW", "FAIL"
TRACK_A, TRACK_B = "PRUNING_DEFENSIVE", "ALPHA_EXPANSION"


def describe(arms: dict) -> dict:
    """两臂的描述性数字。arms：days、base_net、cand_net、base_trades、cand_trades（逐日）。"""
    b, c = arms["base_net"], arms["cand_net"]
    d = stats.paired(b, c)
    n = len(d)
    return {"valid_days": n, "mean_diff": (sum(d) / n) if n else None,
            "base_net": sum(b), "cand_net": sum(c), "base_trades": sum(arms["base_trades"]),
            "cand_trades": sum(arms["cand_trades"]), "base_max_drawdown": stats.max_drawdown(b),
            "cand_max_drawdown": stats.max_drawdown(c), "diff": d}


def screen(arms: dict, cfg: EvalConfig, *, required_days: int, errors: list[str] = ()) -> dict:
    """20 日盲筛：只淘汰，不做肯定性推断。不算置信界。"""
    s = describe(arms)
    reasons = list(errors)
    if errors:
        status = REJECTED
    elif s["valid_days"] < required_days:
        status, reasons = INSUFFICIENT, [f"VALID_DAYS_SHORT:{s['valid_days']}/{required_days}"]
    else:
        if s["mean_diff"] < cfg.screen_reject_mean_diff:
            reasons.append("PAIRED_NET_R_DEGRADED")
        if s["cand_max_drawdown"] - s["base_max_drawdown"] > cfg.screen_reject_drawdown_increase:
            reasons.append("DRAWDOWN_WORSENED")
        status = REJECTED if reasons else CONTINUE
    s.pop("diff")
    return {"status": status, "reasons": reasons, "valid_days": s["valid_days"], "detail": s,
            "thresholds": {"reject_mean_diff_r_per_day": cfg.screen_reject_mean_diff,
                           "reject_drawdown_increase_r": cfg.screen_reject_drawdown_increase,
                           "required_days": required_days}}


def _gate(name, passed, value, threshold, denominator, note=None) -> dict:
    return {"gate": name, "passed": bool(passed), "value": value, "threshold": threshold, "denominator": denominator,
            "note": note}


def confirm(arms: dict, track: str, power, cfg: EvalConfig, *, required_days: int, stress_r_per_trade: float,
            regression_ok: bool = True) -> dict:
    """正式确认。power 是预登记的 PowerConfig（阈值的唯一来源）。"""
    s = describe(arms)
    n, alpha = s["valid_days"], power.alpha
    bound = stats.block_bound(s["diff"], alpha=alpha, seed=cfg.seed, block=cfg.block_length_days,
                              resamples=cfg.resamples, min_tail=cfg.min_tail_count, max_resamples=cfg.max_resamples)
    gates, insufficient = [], []
    gates.append(_gate("REGRESSION_ZERO_FAILURES", regression_ok, None, None, None))
    gates.append(_gate("VALID_DAYS", n >= required_days, n, required_days, n))
    if n < required_days:
        insufficient.append(f"VALID_DAYS_SHORT:{n}/{required_days}")
    few = min(s["base_trades"], s["cand_trades"])
    gates.append(_gate("MIN_TRADES_PER_ARM", few >= power.min_trades_per_arm, few, power.min_trades_per_arm, n,
                       f"基线 {s['base_trades']}，候选 {s['cand_trades']}"))
    if few < power.min_trades_per_arm:
        insufficient.append(f"TRADES_SHORT:{few}/{power.min_trades_per_arm}")
    if bound["bound"] is None:
        insufficient.append(f"BOUND_NOT_ESTIMABLE:{bound['reason']}")
    lb = bound["bound"]
    dd_b, dd_c = s["base_max_drawdown"], s["cand_max_drawdown"]
    if track == TRACK_A:
        gates.append(_gate("NON_INFERIORITY_LOWER_BOUND", lb is not None and lb > -power.margin_r_per_day, lb,
                           -power.margin_r_per_day, n, f"单侧 α={alpha:g}，均差 {s['mean_diff']}"))
        improve = ((dd_b - dd_c) / dd_b * 100) if dd_b > 0 else 0.0
        gates.append(_gate("DRAWDOWN_IMPROVEMENT_PCT", improve >= power.drawdown_improvement_min_pct, improve,
                           power.drawdown_improvement_min_pct, n, f"基线 {dd_b}，候选 {dd_c}"))
    elif track == TRACK_B:
        gates.append(_gate("MEAN_PAIRED_UPLIFT", s["mean_diff"] is not None and s["mean_diff"] >= power.delta_r_per_day,
                           s["mean_diff"], power.delta_r_per_day, n))
        gates.append(_gate("LOWER_BOUND_ABOVE_ZERO", lb is not None and lb > 0, lb, 0.0, n, f"单侧 α={alpha:g}"))
        gates.append(_gate("DRAWDOWN_INCREASE_R", dd_c - dd_b <= power.drawdown_increase_max_r, dd_c - dd_b,
                           power.drawdown_increase_max_r, n, f"基线 {dd_b}，候选 {dd_c}"))
    else:
        raise ValueError(f"未知的验证轨道：{track}")
    cs = s["cand_net"] - stress_r_per_trade * s["cand_trades"]
    bs = s["base_net"] - stress_r_per_trade * s["base_trades"]
    gates.append(_gate("STRESS_NET_POSITIVE", cs > 0, cs, 0.0, s["cand_trades"], f"每笔多扣 {stress_r_per_trade:g} R"))
    gates.append(_gate("STRESS_NOT_WORSE_THAN_BASE", cs >= bs, cs - bs, 0.0, n, f"基线压力后 {bs}"))
    failed = [g["gate"] for g in gates if not g["passed"]]
    if not regression_ok:
        status, reasons = FAIL, ["REGRESSION_FAILED"]
    elif insufficient:
        status, reasons = INSUFFICIENT, insufficient
    elif failed:
        status, reasons = FAIL, failed
    else:
        status, reasons = PASS, []
    s.pop("diff")
    return {"status": status, "reasons": reasons, "valid_days": n, "gates": gates,
            "gates_passed": {g["gate"]: g["passed"] for g in gates}, "bound": bound, "detail": s, "track": track}


def labeling(samples: list[dict], cfg: EvalConfig, *, regression_ok: bool, logic_unchanged: bool) -> dict:
    """解释／纪律类与 Memory 类：识别率、误报率、引用正确率。samples：expected、predicted、citations_total、citations_correct。"""
    tp = sum(1 for x in samples if x["expected"] and x["predicted"])
    fn = sum(1 for x in samples if x["expected"] and not x["predicted"])
    fp = sum(1 for x in samples if not x["expected"] and x["predicted"])
    tn = sum(1 for x in samples if not x["expected"] and not x["predicted"])
    ct, cc = sum(x["citations_total"] for x in samples), sum(x["citations_correct"] for x in samples)
    rec = tp / (tp + fn) if tp + fn else None
    fpr = fp / (fp + tn) if fp + tn else None
    cit = cc / ct if ct else None
    gates = [_gate("REGRESSION_ZERO_FAILURES", regression_ok, None, None, None),
             _gate("TRADING_LOGIC_UNCHANGED", logic_unchanged, None, None, None),
             _gate("MIN_SAMPLES", len(samples) >= cfg.label_min_samples, len(samples), cfg.label_min_samples, len(samples)),
             _gate("RECOGNITION_RATE", rec is not None and rec >= cfg.label_min_recognition, rec, cfg.label_min_recognition, tp + fn),
             _gate("FALSE_POSITIVE_RATE", fpr is not None and fpr <= cfg.label_max_false_positive, fpr,
                   cfg.label_max_false_positive, fp + tn),
             _gate("CITATION_ACCURACY", cit is not None and cit >= cfg.label_min_citation, cit, cfg.label_min_citation, ct)]
    failed = [g["gate"] for g in gates if not g["passed"]]
    if not regression_ok or not logic_unchanged:
        status = "FAILED"
    elif len(samples) < cfg.label_min_samples or None in (rec, fpr, cit):
        status = INSUFFICIENT
    else:
        status = "FAILED" if failed else "PASSED"
    return {"status": status, "reasons": failed, "gates": gates, "samples": len(samples),
            "counts": {"tp": tp, "fn": fn, "fp": fp, "tn": tn, "citations_total": ct, "citations_correct": cc}}
