"""配置加载。数据根目录和保留集必须在项目之外。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCOPE = "pa_brooks:ES:RTH:5m"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    root: str
    point_value_usd: Decimal
    tick_size: Decimal
    months: tuple[str, ...]
    roll_rule_version: str

    @property
    def tick_value_usd(self) -> Decimal:
        return self.point_value_usd * self.tick_size


@dataclass(frozen=True)
class Scope:
    strategy_family: str
    instrument_root: str
    session_type: str
    timeframe: str

    def __str__(self) -> str:
        return f"{self.strategy_family}:{self.instrument_root}:{self.session_type}:{self.timeframe}"


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_root: Path
    runtime_db: Path
    release_db: Path
    holdout_root: Path
    available_at_delay_seconds: int
    source_priority: tuple[str, ...]
    dev_visible_through: date | None
    instruments: dict[str, Instrument]
    roll_rules: dict[str, dict]
    watch: dict = field(default_factory=dict)
    collect: dict = field(default_factory=dict)

    def instrument(self, root: str) -> Instrument:
        try:
            return self.instruments[root]
        except KeyError:
            raise ConfigError(f"品种未登记：{root}（已登记：{', '.join(sorted(self.instruments))}）") from None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_instruments(path: Path) -> tuple[dict[str, Instrument], dict[str, dict]]:
    raw = _yaml(path)
    rules = raw.get("roll_rules") or {}
    out = {}
    for root, spec in (raw.get("instruments") or {}).items():
        if spec["roll_rule_version"] not in rules:
            raise ConfigError(f"{root} 引用了未定义的换月规则 {spec['roll_rule_version']}")
        out[root] = Instrument(root, Decimal(str(spec["point_value_usd"])), Decimal(str(spec["tick_size"])),
                               tuple(spec["months"]), spec["roll_rule_version"])
    return out, rules


def load_watch(path: Path) -> dict:
    raw = _yaml(path)
    interval = raw.get("poll_interval_seconds", 60)
    presets = raw.get("presets", [60, 300])
    for v in [interval, *presets]:
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ConfigError(f"watch 轮询间隔必须是正整数秒：{v!r}")
    return {"poll_interval_seconds": interval, "presets": list(presets),
            "stream_first": bool(raw.get("stream_first", True))}


def load_collect(path: Path) -> dict:
    raw = _yaml(path)
    out = {"timeframe": raw.get("timeframe", "5m"),
           "lookback_trading_days": raw.get("lookback_trading_days", 2),
           "wait_after_close_seconds": raw.get("wait_after_close_seconds", 10),
           "include_eth": bool(raw.get("include_eth", True)),
           "max_bars_per_request": raw.get("max_bars_per_request", 1200)}
    if out["timeframe"] not in ("1m", "5m"):
        raise ConfigError(f"collect.timeframe 只支持 1m 或 5m：{out['timeframe']!r}")
    for k in ("lookback_trading_days", "max_bars_per_request"):
        if isinstance(out[k], bool) or not isinstance(out[k], int) or out[k] <= 0:
            raise ConfigError(f"collect.{k} 必须是正整数：{out[k]!r}")
    w = out["wait_after_close_seconds"]
    if isinstance(w, bool) or not isinstance(w, int) or w < 0:
        raise ConfigError(f"collect.wait_after_close_seconds 不能为负：{w!r}")
    if out["max_bars_per_request"] > 1200:
        raise ConfigError("collect.max_bars_per_request 不能超过 1200")
    return out


def load_settings(project_root: Path | None = None, env: dict | None = None, *,
                  data_root: Path | str | None = None, release_db: Path | str | None = None,
                  dev_visible_through: date | str | None = None) -> Settings:
    """读取 config/。data_root、release_db、dev_visible_through 可由调用方（CLI、测试）覆盖。"""
    root = Path(project_root or PROJECT_ROOT).resolve()
    env = os.environ if env is None else env
    cfg = root / "config"
    paths = _yaml(cfg / "paths.yaml")

    dr = data_root or env.get("TRADING_AGENT_DATA_ROOT") or paths.get("data_root")
    if not dr:
        raise ConfigError("没有配置 data_root")
    dr = Path(str(dr)).expanduser().resolve()
    if _inside(dr, root):
        raise ConfigError(f"数据根目录必须在项目之外：{dr} 位于 {root} 内")

    hr = Path(str(paths.get("holdout_root") or "")).expanduser()
    if not paths.get("holdout_root"):
        raise ConfigError("没有登记 holdout_root")
    if _inside(hr, root):
        raise ConfigError(f"保留集路径必须在项目之外：{hr}")

    rdb = Path(str(release_db)) if release_db else root / str(paths.get("release_db", "releases/release-state.sqlite"))

    delay = paths.get("available_at_delay_seconds", 5)
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
        raise ConfigError(f"available_at_delay_seconds 必须是非负整数：{delay!r}")

    dvt = dev_visible_through if dev_visible_through is not None else paths.get("dev_visible_through")
    if isinstance(dvt, str):
        dvt = date.fromisoformat(dvt)

    instruments, rules = load_instruments(cfg / "instruments.yaml")
    return Settings(
        project_root=root, data_root=dr, runtime_db=dr / "runtime" / "trading_agent.sqlite",
        release_db=rdb.expanduser(), holdout_root=hr.resolve(),
        available_at_delay_seconds=delay,
        source_priority=tuple(paths.get("source_priority") or ("databento", "webull")),
        dev_visible_through=dvt, instruments=instruments, roll_rules=rules,
        watch=load_watch(cfg / "watch.yaml"), collect=load_collect(cfg / "collect.yaml"))


def parse_scope(text: str, settings: Settings) -> Scope:
    parts = text.split(":")
    if len(parts) != 4 or not all(parts):
        raise ConfigError(
            f"scope 格式错误：{text!r}。应为 strategy_family:instrument_root:session_type:timeframe，如 {DEFAULT_SCOPE}")
    fam, root, sess, tf = parts
    settings.instrument(root)
    if sess not in ("RTH", "ETH"):
        raise ConfigError(f"scope 的 session_type 只能是 RTH 或 ETH：{sess!r}")
    if tf != "5m":
        raise ConfigError(f"scope 的 timeframe 首版只支持 5m：{tf!r}")
    return Scope(fam, root, sess, tf)
