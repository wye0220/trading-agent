"""发布政策预设的加载（config/release.yaml）。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from ..config import ConfigError

WINDOW_KINDS = ("OPERATIONAL_ACCEPTANCE", "FORWARD_HOLDOUT")
FREEZE_CAUSES = ("DATA", "DAILY_LOSS", "MANUAL", "MONITOR", "RELEASE", "CONTEXT")


@dataclass(frozen=True)
class ReleaseConfig:
    version: str
    approvers: tuple[str, ...]
    confirm_prefix_chars: int
    evaluation_max_age_days: int
    shadow_max_age_days: int
    shadow: dict
    pre_eval_top_n: int
    min_paper_run_days: int
    regression_groups: dict
    monitor: dict
    freeze: dict
    digest: str


def _int(v, path, lo=1) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ConfigError(f"{path} 应为 ≥{lo} 的整数：{v!r}")
    return v


def _num(v, path, lo=None, hi=None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ConfigError(f"{path} 不是合法的数：{v!r}")
    return float(v)


def parse(raw: Mapping) -> ReleaseConfig:
    ap, sh, ib, mo, fr = (raw.get(k) or {} for k in ("approval", "shadow", "initial_baseline", "monitor", "freeze"))
    users = ap.get("approvers") or []
    if not users or any(not isinstance(u, str) or not u for u in users):
        raise ConfigError("release.approval.approvers 应为非空的用户名列表")
    ni = sh.get("noninferiority") or {}
    shadow = {
        "min_valid_days": _int(sh.get("min_valid_days"), "release.shadow.min_valid_days"),
        "max_valid_days": _int(sh.get("max_valid_days"), "release.shadow.max_valid_days"),
        "min_fills_per_arm": _int(sh.get("min_fills_per_arm"), "release.shadow.min_fills_per_arm"),
        "expected_fill_ratio": _num(sh.get("expected_fill_ratio"), "release.shadow.expected_fill_ratio", 0.01, 1),
        "window_kind": sh.get("window_kind"),
        "max_data_error_days": _int(sh.get("max_data_error_days", 0), "release.shadow.max_data_error_days", 0),
        "noninferiority": {"enabled": bool(ni.get("enabled")),
                           "margin_r_per_day": _num(ni.get("margin_r_per_day", 0.05), "noninferiority.margin_r_per_day", 0),
                           "alpha": _num(ni.get("alpha", 0.05), "noninferiority.alpha", 0.0001, 0.5),
                           "max_drawdown_increase_r": _num(ni.get("max_drawdown_increase_r", 3.0),
                                                           "noninferiority.max_drawdown_increase_r", 0)},
    }
    if shadow["window_kind"] not in WINDOW_KINDS:
        raise ConfigError(f"release.shadow.window_kind 只能是 {WINDOW_KINDS}")
    if shadow["max_valid_days"] < shadow["min_valid_days"]:
        raise ConfigError("release.shadow.max_valid_days 不能小于 min_valid_days")
    groups = ib.get("regression_groups") or {}
    for g in ("engineering", "vectors", "read_only"):
        files = groups.get(g) or []
        if not files or any(not isinstance(x, str) or not x.startswith("tests/") or ".." in x for x in files):
            raise ConfigError(f"release.initial_baseline.regression_groups.{g} 应列出 tests/ 下的测试文件")
    monitor = {
        "rolling_valid_days": _int(mo.get("rolling_valid_days"), "release.monitor.rolling_valid_days"),
        "net_r_sigma_multiple": _num(mo.get("net_r_sigma_multiple"), "release.monitor.net_r_sigma_multiple", 0),
        "drawdown_multiple": _num(mo.get("drawdown_multiple"), "release.monitor.drawdown_multiple", 0),
        "frequency_ratio": _num(mo.get("frequency_ratio"), "release.monitor.frequency_ratio", 1),
        "ambiguous_bar_multiple": _num(mo.get("ambiguous_bar_multiple"), "release.monitor.ambiguous_bar_multiple", 0),
        "ambiguous_bar_floor": _num(mo.get("ambiguous_bar_floor"), "release.monitor.ambiguous_bar_floor", 0, 1),
        "ambiguous_min_trades": _int(mo.get("ambiguous_min_trades"), "release.monitor.ambiguous_min_trades"),
        "notify_command": mo.get("notify_command") or None,
    }
    nc = monitor["notify_command"]
    if nc is not None and (not isinstance(nc, str) or nc.startswith("/") or ".." in nc):
        raise ConfigError("release.monitor.notify_command 应为项目内的相对路径")
    freeze = {k: tuple(fr.get(k) or ()) for k in ("auto_release", "human_release", "approval_release")}
    listed = [c for v in freeze.values() for c in v]
    if sorted(listed) != sorted(FREEZE_CAUSES):
        raise ConfigError(f"release.freeze 的三类解除政策要恰好覆盖 {FREEZE_CAUSES} 各一次")
    body = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return ReleaseConfig(
        version=str(raw.get("version") or ""), approvers=tuple(users),
        confirm_prefix_chars=_int(ap.get("confirm_prefix_chars", 12), "release.approval.confirm_prefix_chars", 6),
        evaluation_max_age_days=_int(ap.get("evaluation_max_age_days"), "release.approval.evaluation_max_age_days"),
        shadow_max_age_days=_int(ap.get("shadow_max_age_days"), "release.approval.shadow_max_age_days"),
        shadow=shadow, pre_eval_top_n=_int((raw.get("pre_eval_shadow") or {}).get("top_n", 3), "release.pre_eval_shadow.top_n"),
        min_paper_run_days=_int(ib.get("min_paper_run_days"), "release.initial_baseline.min_paper_run_days"),
        regression_groups={g: tuple(groups[g]) for g in ("engineering", "vectors", "read_only")},
        monitor=monitor, freeze=freeze, digest=hashlib.sha256(body.encode()).hexdigest())


def load_release(project_root: Path | str) -> ReleaseConfig:
    p = Path(project_root) / "config" / "release.yaml"
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"缺少 {p}") from None
    return parse(raw)
