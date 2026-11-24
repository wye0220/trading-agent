"""截止日之后的实时行情放行（2026-11-23 用户决定，配置在 config/live_data.yaml）。

放行的是：登记的实时来源、登记首日及以后的交易日。这些 K 线进运行库，供常驻纸面运行、影子和发布后监控使用。
研究这一侧照旧看不到它们：切分登记把放行日排除在开发段、向前验证段和胶囊之外；聚合按截止日排除；
评测只用封存数据。放行首日必须晚于任何已登记封存的交易日，否则这个品种不放行（已封存的日期不会因此披露）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .config import ConfigError


@dataclass(frozen=True)
class LiveWindow:
    sources: tuple[str, ...] = ()
    from_day: dict = field(default_factory=dict)        # instrument_root -> 'YYYY-MM-DD'
    conflicts: tuple[str, ...] = ()

    def starts(self, root: str) -> str | None:
        return self.from_day.get(root)

    def allows(self, source: str, root: str, day: str) -> bool:
        f = self.from_day.get(root)
        return f is not None and source in self.sources and day >= f

    def is_live_day(self, root: str, day: str) -> bool:
        f = self.from_day.get(root)
        return f is not None and day >= f

    def describe(self) -> dict:
        return {"sources": list(self.sources), "from_day": dict(self.from_day), "conflicts": list(self.conflicts)}


EMPTY = LiveWindow()


def parse(raw: dict) -> LiveWindow:
    if not raw:
        return EMPTY
    src = raw.get("sources") or []
    if any(not isinstance(s, str) or not s for s in src):
        raise ConfigError("live_data.sources 应为来源名列表")
    days = {}
    for root, d in (raw.get("from_day") or {}).items():
        try:
            days[str(root)] = (d if isinstance(d, date) else date.fromisoformat(str(d))).isoformat()
        except ValueError:
            raise ConfigError(f"live_data.from_day.{root} 不是合法日期：{d!r}") from None
    return LiveWindow(tuple(src), days)


def load(project_root: Path | str) -> LiveWindow:
    p = Path(project_root) / "config" / "live_data.yaml"
    if not p.is_file():
        return EMPTY
    return parse(yaml.safe_load(p.read_text(encoding="utf-8")) or {})


def sealed_through(con: sqlite3.Connection, root: str) -> str | None:
    """已登记封存的最后一个交易日（所有切分登记里的封存段）。"""
    try:
        r = con.execute("SELECT MAX(s.end_day) FROM data_segments s JOIN split_registries r ON r.registry_id=s.registry_id"
                        " WHERE r.instrument_root=? AND s.status='SEALED'", (root,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return r[0] if r else None


def effective(con: sqlite3.Connection, project_root: Path | str, window: LiveWindow | None = None) -> LiveWindow:
    """去掉与已登记封存日期冲突的品种。冲突写在 conflicts 里，由调用方报告。"""
    w = window if window is not None else load(project_root)
    keep, bad = {}, []
    for root, f in w.from_day.items():
        s = sealed_through(con, root)
        if s is not None and f <= s:
            bad.append(f"{root}: 放行首日 {f} 不晚于已封存的 {s}，不放行")
        else:
            keep[root] = f
    return LiveWindow(w.sources, keep, tuple(bad))
