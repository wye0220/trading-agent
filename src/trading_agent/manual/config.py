"""人工轨配置。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from ..config import ConfigError
from ..sessions import parse_utc

RULES = ("NAKED_POSITION", "STOP_MOVED_AWAY", "REENTRY_IN_COOLDOWN", "UNPLANNED_ENTRY", "COUNTERTREND_ADD",
         "DAILY_LOSS_NEAR_LIMIT")
LEVELS = {"NAKED_POSITION": "CRITICAL"}


@dataclass(frozen=True)
class ManualConfig:
    default_account_id: str | None
    plan_ttl_minutes: int
    cooldown_minutes: int
    quiet_window_minutes: int
    plan_coverage_threshold: float
    stop_tolerance_ticks: int
    daily_loss_warn_fraction: float
    strategy_state_max_age_bars: int
    rules: Mapping[str, bool]


def _pos(v, name, lo=1) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ConfigError(f"{name} 应为 ≥{lo} 的整数：{v!r}")
    return v


def _frac(v, name) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 1:
        raise ConfigError(f"{name} 应在 0 到 1 之间：{v!r}")
    return float(v)


def parse_manual(raw: Mapping) -> ManualConfig:
    try:
        rules = {r: True for r in RULES}
        for k, v in (raw.get("rules") or {}).items():
            if k not in rules or not isinstance(v, bool):
                raise ConfigError(f"rules.{k} 不是已知规则的布尔开关")
            rules[k] = v
        acct = raw.get("default_account_id")
        return ManualConfig(
            default_account_id=None if acct in (None, "") else str(acct),
            plan_ttl_minutes=_pos(raw["plan_ttl_minutes"], "plan_ttl_minutes"),
            cooldown_minutes=_pos(raw["cooldown_minutes"], "cooldown_minutes", 0),
            quiet_window_minutes=_pos(raw["quiet_window_minutes"], "quiet_window_minutes", 0),
            plan_coverage_threshold=_frac(raw["plan_coverage_threshold"], "plan_coverage_threshold"),
            stop_tolerance_ticks=_pos(raw["stop_tolerance_ticks"], "stop_tolerance_ticks", 0),
            daily_loss_warn_fraction=_frac(raw["daily_loss_warn_fraction"], "daily_loss_warn_fraction"),
            strategy_state_max_age_bars=_pos(raw["strategy_state_max_age_bars"], "strategy_state_max_age_bars"),
            rules=rules)
    except KeyError as e:
        raise ConfigError(f"人工轨配置缺少字段：{e}") from None


def load_manual(project_root: Path) -> ManualConfig:
    with open(Path(project_root) / "config" / "manual.yaml", encoding="utf-8") as f:
        return parse_manual(yaml.safe_load(f) or {})


def load_news_windows(project_root: Path) -> list[dict]:
    """资讯窗口：[{label, start_utc, end_utc}]（时刻为 epoch 秒）。没有配置时为空列表。"""
    path = Path(project_root) / "config" / "news_windows.yaml"
    if not path.is_file():
        return []
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out = []
    for w in raw.get("windows") or []:
        try:
            a, b = parse_utc(str(w["start_utc"])), parse_utc(str(w["end_utc"]))
            if b <= a or not str(w["label"]).strip():
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ConfigError(f"资讯窗口必须有 label 和先后正确的 start_utc、end_utc：{w!r}") from None
        out.append({"label": str(w["label"]), "start_utc": a, "end_utc": b})
    return sorted(out, key=lambda w: w["start_utc"])
