"""config/evolve.yaml 的读取与校验。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..config import ConfigError

STRATEGY_TRACKS = ("PRUNING_DEFENSIVE", "ALPHA_EXPANSION")
CHEAP = "CHEAP_NON_STRATEGY"


@dataclass(frozen=True)
class Template:
    id: str
    mechanism: str
    reason: str
    track: str
    key: str
    to: Any
    category: str
    target: str
    observed_problem: str
    hypothesis: str
    expected_tradeoff: str
    invariants: tuple[str, ...]
    prior_failure: Mapping[str, str] | None


@dataclass(frozen=True)
class EvolveConfig:
    window_sessions: int
    min_days: int
    max_candidates_per_family: int
    queue_ttl_days: int
    examples_per_side: int
    days_full_weight: int
    unknown_counter_factor: float
    single_bucket_share: float
    single_bucket_factor: float
    shadow_top_n: int
    rollback_trigger: Mapping[str, str]
    non_strategy_whitelist: tuple[str, ...]
    mechanism_map: Mapping[str, tuple[tuple[str, str, str], ...]]     # 家族 -> (机制, 原因, 差异键)
    templates: Mapping[str, tuple[Template, ...]]
    version: str

    def diff_key(self, family: str, mechanism: str, reason: str) -> str | None:
        """先找原因完全相同的登记，再找不限原因的登记。"""
        rules = self.mechanism_map.get(family, ())
        for m, r, k in rules:
            if m == mechanism and r and r == reason:
                return k
        for m, r, k in rules:
            if m == mechanism and not r:
                return k
        return None

    def templates_for(self, family: str) -> tuple[Template, ...]:
        return self.templates.get(family, ())


def _int(raw, key, lo=0):
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ConfigError(f"evolve.yaml：{key} 应为不小于 {lo} 的整数")
    return v


def _frac(raw, key):
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise ConfigError(f"evolve.yaml：queue_score.{key} 应在 0 到 1 之间")
    return float(v)


def parse(raw: Mapping) -> EvolveConfig:
    qs = raw.get("queue_score") or {}
    mm = {}
    for fam, rules in (raw.get("mechanism_map") or {}).items():
        out = []
        for r in rules or []:
            if not r.get("mechanism") or not r.get("diff_key"):
                raise ConfigError(f"evolve.yaml：mechanism_map.{fam} 的每一项都要有 mechanism 和 diff_key")
            out.append((str(r["mechanism"]), str(r.get("reason") or ""), str(r["diff_key"])))
        mm[fam] = tuple(out)
    tpl = {}
    for fam, items in (raw.get("templates") or {}).items():
        out, seen = [], set()
        for t in items or []:
            for k in ("id", "mechanism", "track", "key", "observed_problem", "hypothesis", "expected_tradeoff"):
                if not t.get(k):
                    raise ConfigError(f"evolve.yaml：templates.{fam} 的模板缺少 {k}")
            if "to" not in t:
                raise ConfigError(f"evolve.yaml：模板 {fam}.{t['id']} 缺少 to")
            if t["id"] in seen:
                raise ConfigError(f"evolve.yaml：模板标识重复：{fam}.{t['id']}")
            seen.add(t["id"])
            track = str(t["track"])
            if track not in STRATEGY_TRACKS + (CHEAP,):
                raise ConfigError(f"evolve.yaml：模板 {fam}.{t['id']} 的 track 不合法：{track}")
            cat = str(t.get("category") or ("strategy_rule" if track in STRATEGY_TRACKS else "explanation_discipline"))
            out.append(Template(
                id=str(t["id"]), mechanism=str(t["mechanism"]), reason=str(t.get("reason") or ""), track=track,
                key=str(t["key"]), to=t["to"], category=cat,
                target=str(t.get("target") or ("strategy_config" if cat == "strategy_rule" else "rule_switch")),
                observed_problem=str(t["observed_problem"]), hypothesis=str(t["hypothesis"]),
                expected_tradeoff=str(t["expected_tradeoff"]), invariants=tuple(str(x) for x in t.get("invariants") or ()),
                prior_failure=dict(t["prior_failure"]) if t.get("prior_failure") else None))
        tpl[fam] = tuple(out)
    rb = {str(k): str(v) for k, v in (raw.get("rollback_trigger") or {}).items()}
    for k in STRATEGY_TRACKS + (CHEAP,):
        if not rb.get(k):
            raise ConfigError(f"evolve.yaml：rollback_trigger 缺少 {k}")
    version = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:12]
    return EvolveConfig(
        window_sessions=_int(raw, "window_sessions"), min_days=_int(raw, "min_days", 1),
        max_candidates_per_family=_int(raw, "max_candidates_per_family", 1), queue_ttl_days=_int(raw, "queue_ttl_days", 1),
        examples_per_side=min(3, _int(raw, "examples_per_side", 1)),       # 规格上限：每边最多 3 条
        days_full_weight=_int(qs, "days_full_weight", 1), unknown_counter_factor=_frac(qs, "unknown_counter_factor"),
        single_bucket_share=_frac(qs, "single_bucket_share"), single_bucket_factor=_frac(qs, "single_bucket_factor"),
        shadow_top_n=_int(raw.get("shadow") or {}, "top_n", 1), rollback_trigger=rb,
        non_strategy_whitelist=tuple(str(x) for x in raw.get("non_strategy_whitelist") or ()),
        mechanism_map=mm, templates=tpl, version="evolve-" + version)


def load_evolve(project_root: Path) -> EvolveConfig:
    with open(Path(project_root) / "config" / "evolve.yaml", encoding="utf-8") as f:
        return parse(yaml.safe_load(f) or {})
