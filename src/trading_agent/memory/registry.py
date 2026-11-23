"""Memory 登记表：条目的字符串字段只能取这里登记的值。禁止项写在代码里，配置放行不了。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from ..config import ConfigError

KINDS = ("user_preference", "market_fact", "discipline_pattern")
UNITS = ("s", "yyyymmdd", "ratio", "r", "count")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# 风险上限、权限：只从风控配置读。实时状态：只从账本读。
RISK_LIMIT = re.compile(r"(budget|limit|max_contracts|max_stops|max_position|max_quantity|max_loss|leverage"
                        r"|approved|permission|authoriz|release|risk_unit|quantity_cap)")
LIVE_STATE = re.compile(r"(position|pnl|open_risk|reserved|exposure|equity|balance|buying_power|holdings|open_order)")


def forbidden(name: str) -> str | None:
    """名字指向风险上限或实时状态时给出原因码。"""
    if RISK_LIMIT.search(name):
        return "RISK_LIMIT_FORBIDDEN"
    if LIVE_STATE.search(name):
        return "LIVE_STATE_FORBIDDEN"
    return None


@dataclass(frozen=True)
class Registry:
    version: str
    max_items_per_context: int
    discipline_min_sample_n: int
    preference_keys: Mapping[str, Mapping]
    metrics: Mapping[str, Mapping[str, Mapping]]
    condition_keys: Mapping[str, Mapping]
    sources: Mapping[str, tuple[str, ...]]
    relevance: Mapping[str, Mapping]
    digest: str

    def metric(self, kind: str, name: str) -> Mapping | None:
        if kind == "user_preference":
            return self.preference_keys.get(name)
        return self.metrics.get(kind, {}).get(name)


def _nat(v, path, lo=1) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ConfigError(f"{path} 应为 ≥{lo} 的整数：{v!r}")
    return v


def _name(n, path) -> str:
    if not isinstance(n, str) or not NAME.match(n):
        raise ConfigError(f"{path} 不是合法的名字：{n!r}")
    why = forbidden(n)
    if why:
        raise ConfigError(f"{path} 不能登记进 Memory（{why}）：{n}")
    return n


def _label(spec, path) -> str:
    lab = spec.get("label")
    if not isinstance(lab, str) or not lab or len(lab) > 24 or "\n" in lab:
        raise ConfigError(f"{path}.label 应为不超过 24 个字符的单行标签")
    return lab


def parse(raw: Mapping) -> Registry:
    prefs = {}
    for k, s in (raw.get("preference_keys") or {}).items():
        p = f"memory.preference_keys.{k}"
        _name(k, p)
        lo, hi = s.get("min"), s.get("max")
        if isinstance(lo, bool) or isinstance(hi, bool) or not isinstance(lo, int) or not isinstance(hi, int) or lo > hi:
            raise ConfigError(f"{p} 需要整数的 min 和 max")
        if s.get("unit") not in (None, *UNITS):
            raise ConfigError(f"{p}.unit 未登记：{s.get('unit')!r}")
        prefs[k] = {"min": lo, "max": hi, "unit": s.get("unit"), "label": _label(s, p)}
    metrics = {}
    for kind, ms in (raw.get("metrics") or {}).items():
        if kind not in ("market_fact", "discipline_pattern"):
            raise ConfigError(f"memory.metrics 的 kind 未知：{kind}")
        metrics[kind] = {}
        for k, s in (ms or {}).items():
            p = f"memory.metrics.{kind}.{k}"
            _name(k, p)
            if s.get("unit") not in UNITS:
                raise ConfigError(f"{p}.unit 未登记：{s.get('unit')!r}")
            metrics[kind][k] = {"unit": s["unit"], "label": _label(s, p)}
    conds = {}
    for k, s in (raw.get("condition_keys") or {}).items():
        p = f"memory.condition_keys.{k}"
        _name(k, p)
        spec = {"label": _label(s, p)}
        if "values" in s:
            spec["values"] = tuple(_name(v, f"{p}.values") for v in s["values"])
        elif "int" in s:
            lo, hi = s["int"]
            spec["int"] = (_nat(lo, p, 0), _nat(hi, p, 0))
        elif s.get("contract") is True:
            spec["contract"] = True
        else:
            raise ConfigError(f"{p} 需要 values、int 或 contract 之一")
        conds[k] = spec
    sources = {}
    for kind in KINDS:
        vals = (raw.get("sources") or {}).get(kind) or []
        sources[kind] = tuple(_name(v, f"memory.sources.{kind}") for v in vals)
    rel = {}
    for task, spec in (raw.get("relevance") or {}).items():
        out = {}
        for kind, v in (spec or {}).items():
            if kind not in KINDS:
                raise ConfigError(f"memory.relevance.{task} 的 kind 未知：{kind}")
            if kind == "user_preference":
                if not isinstance(v, list) or any(x not in prefs for x in v):
                    raise ConfigError(f"memory.relevance.{task}.user_preference 应列出已登记的偏好键")
                out[kind] = tuple(v)
            elif v is True:
                out[kind] = True
            else:
                raise ConfigError(f"memory.relevance.{task}.{kind} 只能写 true")
        rel[task] = out
    body = {"preference_keys": prefs, "metrics": metrics, "condition_keys": conds, "sources": sources, "relevance": rel,
            "max": raw.get("max_items_per_context"), "min_n": raw.get("discipline_min_sample_n")}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=list).encode()).hexdigest()
    return Registry(str(raw.get("version") or "memory-registry"), _nat(raw.get("max_items_per_context"), "memory.max_items_per_context"),
                    _nat(raw.get("discipline_min_sample_n"), "memory.discipline_min_sample_n"), prefs, metrics, conds,
                    sources, rel, digest)


def load_registry(project_root: Path | str) -> Registry:
    f = Path(project_root) / "config" / "memory.yaml"
    try:
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"缺少配置文件：{f}") from None
    return parse(raw)
