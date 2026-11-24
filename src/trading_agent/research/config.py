"""config/research.yaml 的加载与校验。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..config import ConfigError

TRACKS = ("PRUNING_DEFENSIVE", "ALPHA_EXPANSION")


@dataclass(frozen=True)
class Perturbation:
    id: str
    track: str
    note: str
    diff: Mapping[str, Any]


@dataclass(frozen=True)
class PowerConfig:
    seed: int
    block_length_days: int
    simulations: int
    inner_resamples: int
    confirm_days: int
    alpha_family: float
    max_formal_tests: int
    power_target: float
    min_trades_per_arm: int
    min_dev_days: int
    stress_extra_ticks_per_side: int
    margin_r_per_day: float
    drawdown_improvement_min_pct: float
    delta_r_per_day: float
    planning_true_effect: float
    drawdown_increase_max_r: float
    true_effect_grid: tuple[float, ...]

    @property
    def alpha(self) -> float:
        """每次正式检验实际分配到的单侧 α（Bonferroni）。"""
        return self.alpha_family / self.max_formal_tests


@dataclass(frozen=True)
class ResearchConfig:
    forward_validation_start: date | None
    capsule_valid_days: int
    warmup_trading_days: int
    blind_screen_capsules: int
    confirm_capsules: int
    group_by: Mapping[str, Mapping[str, str]]
    time_buckets_et: tuple[str, ...]
    power: PowerConfig
    perturbations: Mapping[str, tuple[Perturbation, ...]]
    version: str

    def groups_for(self, family: str) -> Mapping[str, str]:
        return self.group_by.get(family, {})

    def perturbations_for(self, family: str) -> tuple[Perturbation, ...]:
        return self.perturbations.get(family, ())


def _pos_int(v, path, lo=1) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ConfigError(f"{path} 应为 ≥{lo} 的整数：{v!r}")
    return v


def _num(v, path, lo=0.0, hi=None, strict=False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{path} 应为数字：{v!r}")
    f = float(v)
    if f < lo or (strict and f <= lo) or (hi is not None and f > hi):
        raise ConfigError(f"{path} 超出允许范围：{v!r}")
    return f


def parse(raw: Mapping) -> ResearchConfig:
    try:
        sp, rp, pw = raw["splits"], raw["report"], raw["power"]
        fvs = sp.get("forward_validation_start")
        if isinstance(fvs, str):
            fvs = date.fromisoformat(fvs)
        if fvs is not None and not isinstance(fvs, date):
            raise ConfigError(f"splits.forward_validation_start 应为日期：{fvs!r}")
        buckets = tuple(str(b) for b in rp.get("time_buckets_et") or ())
        for b in buckets:
            hh, _, mm = b.partition(":")
            if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
                raise ConfigError(f"report.time_buckets_et 应为 HH:MM：{b!r}")
        if list(buckets) != sorted(buckets):
            raise ConfigError("report.time_buckets_et 应按时间升序")
        group_by = {}
        for fam, items in (rp.get("group_by") or {}).items():
            if not isinstance(items, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in items.items()):
                raise ConfigError(f"report.group_by.{fam} 应为 标签 → 元数据路径")
            group_by[fam] = dict(items)
        grid = tuple(_num(x, "power.true_effect_grid_r_per_day") for x in pw["true_effect_grid_r_per_day"])
        if not grid or list(grid) != sorted(set(grid)):
            raise ConfigError("power.true_effect_grid_r_per_day 应为升序且不重复")
        power = PowerConfig(
            seed=_pos_int(pw["seed"], "power.seed", 0),
            block_length_days=_pos_int(pw["block_length_days"], "power.block_length_days"),
            simulations=_pos_int(pw["simulations"], "power.simulations", 10),
            inner_resamples=_pos_int(pw["inner_resamples"], "power.inner_resamples", 10),
            confirm_days=_pos_int(pw["confirm_days"], "power.confirm_days", 2),
            alpha_family=_num(pw["alpha_family"], "power.alpha_family", 0.0, 0.5, strict=True),
            max_formal_tests=_pos_int(pw["max_formal_tests"], "power.max_formal_tests"),
            power_target=_num(pw["power_target"], "power.power_target", 0.0, 1.0, strict=True),
            min_trades_per_arm=_pos_int(pw["min_trades_per_arm"], "power.min_trades_per_arm", 0),
            min_dev_days=_pos_int(pw["min_dev_days"], "power.min_dev_days"),
            stress_extra_ticks_per_side=_pos_int(pw["stress_extra_ticks_per_side"], "power.stress_extra_ticks_per_side", 0),
            margin_r_per_day=_num(pw["track_a"]["margin_r_per_day"], "power.track_a.margin_r_per_day"),
            drawdown_improvement_min_pct=_num(pw["track_a"]["drawdown_improvement_min_pct"],
                                              "power.track_a.drawdown_improvement_min_pct", 0.0, 100.0),
            delta_r_per_day=_num(pw["track_b"]["delta_r_per_day"], "power.track_b.delta_r_per_day", 0.0, strict=True),
            planning_true_effect=_num(pw["track_b"].get("planning_true_effect_r_per_day", pw["track_b"]["delta_r_per_day"]),
                                      "power.track_b.planning_true_effect_r_per_day", 0.0, strict=True),
            drawdown_increase_max_r=_num(pw["track_b"]["drawdown_increase_max_r"], "power.track_b.drawdown_increase_max_r"),
            true_effect_grid=grid)
        perts = {}
        for fam, items in (raw.get("perturbations") or {}).items():
            seen, out = set(), []
            for it in items or ():
                pid, track, diff = it["id"], it["track"], it["diff"]
                if not isinstance(pid, str) or not pid.replace("_", "").isalnum():
                    raise ConfigError(f"perturbations.{fam}：id 只能含字母、数字和下划线：{pid!r}")
                if pid in seen:
                    raise ConfigError(f"perturbations.{fam}：id 重复：{pid}")
                if track not in TRACKS:
                    raise ConfigError(f"perturbations.{fam}.{pid}.track 只能是 {TRACKS}")
                if not isinstance(diff, Mapping) or not diff:
                    raise ConfigError(f"perturbations.{fam}.{pid}.diff 不能为空")
                seen.add(pid)
                out.append(Perturbation(pid, track, str(it.get("note") or ""), dict(diff)))
            perts[fam] = tuple(out)
        usage = sp.get("default_usage") or {}
        version = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:12]
        return ResearchConfig(
            forward_validation_start=fvs,
            capsule_valid_days=_pos_int(sp["capsule_valid_days"], "splits.capsule_valid_days"),
            warmup_trading_days=_pos_int(sp.get("warmup_trading_days", 0), "splits.warmup_trading_days", 0),
            blind_screen_capsules=_pos_int(usage.get("blind_screen_capsules", 1), "splits.default_usage.blind_screen_capsules"),
            confirm_capsules=_pos_int(usage.get("confirm_capsules", 2), "splits.default_usage.confirm_capsules"),
            group_by=group_by, time_buckets_et=buckets, power=power, perturbations=perts,
            version="research-" + version)
    except (KeyError, TypeError) as e:
        raise ConfigError(f"研究配置缺少或写错了字段：{e}") from None


def load_research(project_root: Path) -> ResearchConfig:
    with open(Path(project_root) / "config" / "research.yaml", encoding="utf-8") as f:
        return parse(yaml.safe_load(f) or {})


def validate_perturbations(cfg: ResearchConfig, registry, family: str) -> dict:
    """用策略登记的变体工厂试构造每个扰动：差异触及白名单之外的键、或构造出非法配置都会在这里报错。"""
    out = {}
    for p in cfg.perturbations_for(family):
        try:
            out[p.id] = registry.variant(family, p.diff, p.id)
        except Exception as e:  # noqa: BLE001 — 配置错误统一转成 ConfigError
            raise ConfigError(f"扰动 {family}.{p.id} 不合法：{e}") from None
    return out
