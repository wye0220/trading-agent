"""告警分发：alerts 表里的告警按级别、去重键和静默窗口推送到通知适配器，分发结果记在 alert_deliveries。

第一次分发时，之前积压的告警标为 BACKLOG、不推送，免得一次把历史告警全发出去。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from typing import Callable

from ..config import Settings
from . import state
from .config import OpsConfig

FROM_KEY = "alerts:dispatch_from_id"


def dedup_key(level: str, kind: str, scope: str | None, detail: dict) -> str:
    sub = detail.get("position_key") or detail.get("account_id") or detail.get("request_id") or ""
    return "|".join([level, kind, scope or "", str(sub)])


def notifier_for(settings: Settings, cfg: OpsConfig) -> Callable[[str], str]:
    if cfg.notifier == "none":
        return lambda text: "NONE"
    script = settings.project_root / "ops" / "notify-telegram.sh"

    def send(text: str) -> str:
        if not script.is_file():
            return "FAILED:NO_SCRIPT"
        try:
            r = subprocess.run([str(script), text], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return f"FAILED:{type(e).__name__}"
        return "SENT" if r.returncode == 0 else f"FAILED:{r.returncode}"
    return send


def _format(r) -> str:
    return f"[{r['level']}] trading-agent {r['kind']}" + (f" {r['scope']}" if r["scope"] else "") + f"：{r['message']}"


def dispatch(con: sqlite3.Connection, cfg: OpsConfig, notify: Callable[[str], str], *, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    start = state.get(con, FROM_KEY)
    if start is None:
        mx = con.execute("SELECT COALESCE(MAX(alert_id), 0) FROM alerts").fetchone()[0]
        with con:
            con.execute("INSERT OR IGNORE INTO alert_deliveries (alert_id, dedup_key, status, channel, detail, created_at_utc)"
                        " SELECT alert_id, level||'|'||kind||'|'||COALESCE(scope,''), 'BACKLOG', 'none',"
                        " '启用分发之前的告警，不推送', ? FROM alerts WHERE alert_id<=?", (now, mx))
        state.put(con, FROM_KEY, mx, now)
    rows = con.execute("SELECT a.* FROM alerts a LEFT JOIN alert_deliveries d ON d.alert_id=a.alert_id"
                       " WHERE d.alert_id IS NULL ORDER BY a.alert_id").fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except ValueError:
            detail = {}
        key = dedup_key(r["level"], r["kind"], r["scope"], detail)
        if r["level"] not in cfg.push_levels:
            status, info = "NOT_PUSHED", "级别不在 push_levels 里"
        else:
            recent = con.execute("SELECT 1 FROM alert_deliveries WHERE dedup_key=? AND status='SENT' AND created_at_utc>?",
                                 (key, now - cfg.silence_for(r["kind"]))).fetchone()
            if recent:
                status, info = "SUPPRESSED", f"静默窗口 {cfg.silence_for(r['kind'])} 秒内已推送过"
            else:
                res = notify(_format(r))
                status, info = ("SENT", None) if res in ("SENT", "NONE") else ("FAILED", res)
        with con:
            con.execute("INSERT INTO alert_deliveries VALUES (?,?,?,?,?,?)",
                        (r["alert_id"], key, status, cfg.notifier, info, now))
        counts[status] = counts.get(status, 0) + 1
    return {"status": "OK", "dispatched": len(rows), "by_status": counts}


def watch_check(con: sqlite3.Connection, settings: Settings, account_id: str | None, now: int) -> dict:
    """交易时段内 watch 心跳过期：CRITICAL WATCH_STOPPED（持仓失去盘中提醒）。"""
    from .. import sessions
    from ..manual import watch as watch_mod
    from ..scan import _session_for
    if not account_id:
        return {"status": "NO_ACCOUNT"}
    day, in_rth = _session_for(now)
    hb = watch_mod.heartbeat(con, account_id)
    interval = (state.get(con, "watch_interval_override") or {}).get("seconds") or settings.watch["poll_interval_seconds"]
    limit = watch_mod.stale_after(int(interval))
    age = None if hb is None else now - int(hb.get("at_utc") or 0)
    if not in_rth or not sessions.is_trading_day(day):
        return {"status": "OUT_OF_SESSION", "heartbeat_age_seconds": age}
    if age is None or age > limit:
        state.alert(con, "CRITICAL", "WATCH_STOPPED",
                    f"watch 已停止或数据过期（心跳 {'从未' if age is None else f'{age} 秒前'}，阈值 {limit} 秒）：持仓失去盘中提醒",
                    detail={"account_id": account_id, "heartbeat_age_seconds": age, "stale_after_seconds": limit}, now=now)
        return {"status": "WATCH_STOPPED", "heartbeat_age_seconds": age, "stale_after_seconds": limit}
    return {"status": "OK", "heartbeat_age_seconds": age, "stale_after_seconds": limit}
