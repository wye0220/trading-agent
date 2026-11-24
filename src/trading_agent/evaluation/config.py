"""评测协议预设的加载。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from ..config import ConfigError

SCREEN_FIELDS = ("status", "reasons", "valid_days")
CONFIRM_FIELDS = ("status", "reasons", "valid_days", "gates_passed")


@dataclass(frozen=True)
class EvalConfig:
    version: str
    warmup_trading_days: int
    seed: int
    block_length_days: int
    resamples: int
    min_tail_count: int
    max_resamples: int
    screen_reject_mean_diff: float
    screen_reject_drawdown_increase: float
    label_min_samples: int
    label_min_recognition: float
    label_max_false_positive: float
    label_min_citation: float
    visible_screen: tuple[str, ...]
    visible_confirm: tuple[str, ...]
    synthetic_regression: tuple[str, ...]
    author_users: tuple[str, ...]
    sealed_data_root: Path | None
    digest: str

    def body(self) -> dict:
        """进入协议正文的部分（不含身份和路径）。"""
        return {"version": self.version, "warmup_trading_days": self.warmup_trading_days,
                "bootstrap": {"seed": self.seed, "block_length_days": self.block_length_days,
                              "resamples": self.resamples, "min_tail_count": self.min_tail_count,
                              "max_resamples": self.max_resamples},
                "screen": {"reject_mean_diff_r_per_day": self.screen_reject_mean_diff,
                           "reject_drawdown_increase_r": self.screen_reject_drawdown_increase},
                "labeling": {"min_samples": self.label_min_samples, "min_recognition_rate": self.label_min_recognition,
                             "max_false_positive_rate": self.label_max_false_positive,
                             "min_citation_accuracy": self.label_min_citation},
                "visible_summary": {"screen": list(self.visible_screen), "confirm": list(self.visible_confirm)},
                "synthetic_regression": list(self.synthetic_regression)}


def _int(v, path, lo=1) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ConfigError(f"{path} 应为 ≥{lo} 的整数：{v!r}")
    return v


def _num(v, path, lo=None, hi=None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ConfigError(f"{path} 不是合法的数：{v!r}")
    return float(v)


def parse(raw: Mapping) -> EvalConfig:
    b, s, lab = raw.get("bootstrap") or {}, raw.get("screen") or {}, raw.get("labeling") or {}
    vis, iso = raw.get("visible_summary") or {}, raw.get("isolation") or {}
    for key, allowed in (("screen", SCREEN_FIELDS), ("confirm", CONFIRM_FIELDS)):
        extra = set(vis.get(key) or ()) - set(allowed)
        if extra or "status" not in (vis.get(key) or ()):
            raise ConfigError(f"evaluation.visible_summary.{key} 只能取 {allowed}，且必须含 status")
    reg = raw.get("synthetic_regression") or []
    if not reg or any(not isinstance(x, str) or not x.startswith("tests/") or ".." in x for x in reg):
        raise ConfigError("evaluation.synthetic_regression 应列出 tests/ 下的测试文件")
    users = iso.get("author_users") or []
    if any(not isinstance(u, str) or not u for u in users):
        raise ConfigError("evaluation.isolation.author_users 应为用户名列表")
    sealed = iso.get("sealed_data_root")
    cfg = dict(
        version=str(raw.get("version") or "eval-protocol"),
        warmup_trading_days=_int(raw.get("warmup_trading_days"), "evaluation.warmup_trading_days", 0),
        seed=_int(b.get("seed"), "evaluation.bootstrap.seed", 0),
        block_length_days=_int(b.get("block_length_days"), "evaluation.bootstrap.block_length_days"),
        resamples=_int(b.get("resamples"), "evaluation.bootstrap.resamples", 100),
        min_tail_count=_int(b.get("min_tail_count"), "evaluation.bootstrap.min_tail_count"),
        max_resamples=_int(b.get("max_resamples"), "evaluation.bootstrap.max_resamples", 100),
        screen_reject_mean_diff=_num(s.get("reject_mean_diff_r_per_day"), "evaluation.screen.reject_mean_diff_r_per_day", hi=0),
        screen_reject_drawdown_increase=_num(s.get("reject_drawdown_increase_r"), "evaluation.screen.reject_drawdown_increase_r", lo=0),
        label_min_samples=_int(lab.get("min_samples"), "evaluation.labeling.min_samples"),
        label_min_recognition=_num(lab.get("min_recognition_rate"), "evaluation.labeling.min_recognition_rate", 0, 1),
        label_max_false_positive=_num(lab.get("max_false_positive_rate"), "evaluation.labeling.max_false_positive_rate", 0, 1),
        label_min_citation=_num(lab.get("min_citation_accuracy"), "evaluation.labeling.min_citation_accuracy", 0, 1),
        visible_screen=tuple(vis["screen"]), visible_confirm=tuple(vis["confirm"]),
        synthetic_regression=tuple(reg), author_users=tuple(users),
        sealed_data_root=Path(str(sealed)).expanduser() if sealed else None)
    if cfg["max_resamples"] < cfg["resamples"]:
        raise ConfigError("evaluation.bootstrap.max_resamples 不能小于 resamples")
    tmp = EvalConfig(**cfg, digest="")
    digest = hashlib.sha256(json.dumps(tmp.body(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return EvalConfig(**cfg, digest=digest)


def load_eval(project_root: Path | str) -> EvalConfig:
    f = Path(project_root) / "config" / "evaluation.yaml"
    try:
        return parse(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
    except FileNotFoundError:
        raise ConfigError(f"缺少配置文件：{f}") from None
