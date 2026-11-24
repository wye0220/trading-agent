"""盘中节拍：5 分钟线收线且可用后，对每个登记的 scope 扫描一次并推进影子。

去重靠水位（已处理的最后一根线的收线时刻）和运行锁。睡眠或停机之后补跑时，引擎把落下的信号只记进历史，
不会把过期的挂单恢复成当前入场。
"""
from __future__ import annotations

import sqlite3
import time

from .. import contracts, sessions
from ..config import Settings, parse_scope
from ..scan import _session_for
from . import state
from .config import OpsConfig

LOCK = "tick"


def latest_bar(con: sqlite3.Connection, settings: Settings, scope, now: int, source: str | None = None
               ) -> tuple[str, int | None]:
    day, _ = _session_for(now)
    inst = settings.instrument(scope.instrument_root)
    cid = contracts.active_contract(inst, day, settings.roll_rules)
    r = con.execute("SELECT MAX(bar_end_utc) FROM bars WHERE contract_id=? AND timeframe=? AND trading_day=?"
                    " AND session_type='RTH' AND available_at_utc<=? AND bar_end_utc<=?"
                    + (" AND source=?" if source else ""),
                    (cid, scope.timeframe, day.isoformat(), now, now, *((source,) if source else ()))).fetchone()
    return day.isoformat(), r[0] if r else None


def primary_source(settings: Settings) -> str | None:
    from ..tvfeed import config as backup_config
    return (backup_config(settings.project_root) or {}).get("primary_source")


def lateness(scope, day: str, latest: int | None, now: int, grace: int) -> int:
    """盘中此刻应已到、却还没到的 5 分钟线根数。盘外为 0。"""
    from datetime import date
    d = date.fromisoformat(day)
    o, c = sessions.rth_bounds(d)
    if not (o <= now < c + grace):
        return 0
    due = [s + 300 for s in sessions.expected_rth_slots(d) if s + 300 + grace <= now]
    if not due:
        return 0
    have = latest or o
    return sum(1 for e in due if e > have)


def run(settings: Settings, con: sqlite3.Connection, cfg: OpsConfig, *, registry, risk_cfg, engine_cfg, release_cfg,
        now: int | None = None, shadow: bool = True) -> dict:
    from .. import scanner
    from ..release import pointer
    from ..release import shadow as rshadow
    now = int(time.time()) if now is None else now
    if not state.acquire(con, LOCK, cfg.lock_ttl_seconds, now):
        return {"status": "LOCKED", "as_of_utc": now}
    out = []
    try:
        for text in cfg.scopes:
            scope = parse_scope(text, settings)
            day, latest = latest_bar(con, settings, scope, now)
            late = lateness(scope, day, latest, now, cfg.grace_seconds)
            if late > cfg.late_bars_critical:
                state.alert(con, "CRITICAL", "DATA_STALE", f"{scope} 行情断流：盘中已落后 {late} 根 5 分钟线",
                            scope=text, detail={"late_bars": late, "latest_bar_end_utc": latest}, now=now)
            elif late > 0 and late >= cfg.late_bars_warn:
                state.alert(con, "WARN", "LATE_BAR", f"{scope} 5 分钟线迟到 {late} 根", scope=text,
                            detail={"late_bars": late, "latest_bar_end_utc": latest}, now=now)
            prim = primary_source(settings)
            if prim and latest is not None and lateness(scope, day, latest, now, cfg.grace_seconds) == 0:
                _, plat = latest_bar(con, settings, scope, now, prim)
                if plat is None:
                    # 主来源当天没有 RTH 线，引擎选了备用来源：选中它的日子记为降级日
                    state.alert(con, "WARN", "BACKUP_SOURCE_IN_USE",
                                f"{scope} 主来源 {prim} 今天没有 RTH 线，正在用备用行情（tvfeed，当天记为降级日）",
                                scope=text, detail={"primary": prim}, now=now)
            wm = state.get(con, f"watermark:{text}") or {}
            if latest is None or latest <= (wm.get("bar_end_utc") or 0):
                out.append({"scope": text, "status": "NO_NEW_BAR", "trading_day": day, "late_bars": late,
                            "watermark": wm.get("bar_end_utc")})
                continue
            ro = pointer.open_readonly(settings.release_db)
            try:
                r = scanner.live_scan(settings, con, scope, registry.get(scope.strategy_family), now_utc=now,
                                      risk_cfg=risk_cfg, engine_cfg=engine_cfg, release_con=ro, registry=registry)
            finally:
                if ro is not None:
                    ro.close()
            sh = None
            if shadow:
                sh = rshadow.step(settings, con, scope, release_cfg, registry=registry, risk_cfg=risk_cfg,
                                  engine_cfg=engine_cfg, now_utc=now)
            state.put(con, f"watermark:{text}", {"bar_end_utc": latest, "trading_day": day, "scanned_at_utc": now}, now)
            out.append({"scope": text, "status": "SCANNED", "trading_day": day, "late_bars": late, "bar_end_utc": latest,
                        "signal_status": r.get("signal_status"), "risk_status": r.get("risk_status"),
                        "block_reason": r.get("block_reason"), "release": (r.get("release") or {}).get("release_id"),
                        "shadows": len((sh or {}).get("shadows") or []),
                        "pre_eval_shadow": len((sh or {}).get("pre_eval_shadow") or [])})
        state.heartbeat(con, "tick", now, scopes=[x["scope"] for x in out])
        return {"status": "OK", "as_of_utc": now, "scopes": out}
    finally:
        state.release(con, LOCK)
