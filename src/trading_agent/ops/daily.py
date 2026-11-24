"""盘后与周度流水。

盘后（收盘后 after_close_minutes，提前收盘随日历）：确认当日线到齐 → 封存并复盘 → 账本检查 → 发布后监控。
前向保留窗口里的交易日不复盘（复盘产物属于评测侧），只记运行健康。
周度：每周最后一个交易日按 scope 运行聚合。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import date

from .. import sessions
from ..config import Settings, parse_scope
from ..manual.common import paper_run_id
from ..scan import _session_for
from . import state
from .config import OpsConfig


def _day(now: int, day: str | None) -> date:
    return date.fromisoformat(day) if day else _session_for(now)[0]


def run_daily(settings: Settings, con: sqlite3.Connection, cfg: OpsConfig, *, registry, risk_cfg, engine_cfg,
              release_cfg, manual_cfg, day: str | None = None, now: int | None = None) -> dict:
    from ..release import manifest as rman
    from ..release import monitor as rmon
    from ..release import pointer
    from ..release.shadow import reserved_window_start
    from ..review import build as review
    now = int(time.time()) if now is None else now
    d = _day(now, day)
    if not sessions.is_trading_day(d):
        return {"status": "NOT_TRADING_DAY", "trading_day": d.isoformat()}
    if now < sessions.rth_bounds(d)[1] + cfg.after_close_minutes * 60:
        return {"status": "TOO_EARLY", "trading_day": d.isoformat(),
                "runs_after_utc": sessions.rth_bounds(d)[1] + cfg.after_close_minutes * 60}
    ds = d.isoformat()
    from .. import live_window
    from . import tick as otick
    lw = live_window.effective(con, settings.project_root)
    cut = settings.dev_visible_through.isoformat() if settings.dev_visible_through else None
    scopes = []
    for text in cfg.scopes:
        scope = parse_scope(text, settings)
        item = {"scope": text}
        if cut and ds > cut and not lw.is_live_day(scope.instrument_root, ds):
            item["status"] = "SEALED_DAY"                  # 截止日之后、放行首日之前：数据是封存的，不复盘、不告警
            scopes.append(item)
            continue
        td = con.execute("SELECT complete, degraded FROM trading_days WHERE instrument_root=? AND trading_day=?",
                         (scope.instrument_root, ds)).fetchone()
        if td is None or not td[0]:
            state.alert(con, "WARN", "INCOMPLETE_DAY", f"{scope} {ds} 的 RTH 线没有到齐，没有封存和复盘", scope=text, now=now)
            item["status"] = "INCOMPLETE_DAY"
            scopes.append(item)
            continue
        # 先补扫当天：没有盘中节拍时，纸面运行要到这里才看到当天的线。补扫的信号已过期，不挂单，
        # 也不计入纸面运行验收（只数按时处理的交易日）。
        try:
            item["catch_up"] = otick.run(settings, con, cfg, registry=registry, risk_cfg=risk_cfg, engine_cfg=engine_cfg,
                                         release_cfg=release_cfg, now=now)["status"]
        except Exception as e:  # noqa: BLE001
            item["catch_up"] = f"ERROR:{type(e).__name__}"
        run_id = paper_run_id(scope)
        has_run = con.execute("SELECT 1 FROM engine_runs WHERE run_id=?", (run_id,)).fetchone() is not None
        hold = reserved_window_start(con, text)
        if not has_run:
            item["review"] = "NO_PAPER_RUN"
        elif hold and ds >= hold:
            item["review"] = "SKIPPED_FORWARD_HOLDOUT"
        else:
            try:
                r = review.run(settings, con, scope, run_id, ds, seal_first=True)
                item["review"] = r.get("status", "OK") if isinstance(r, dict) else "OK"
            except Exception as e:  # noqa: BLE001 — 复盘失败不挡监控
                code = getattr(e, "code", None)
                if code == "NO_ACTIVITY":
                    item["review"] = "NO_ACTIVITY"
                else:
                    item["review"] = f"ERROR:{code or type(e).__name__}"
                    state.alert(con, "WARN", "REVIEW_FAILED", f"{scope} {ds} 复盘失败：{e}", scope=text, now=now)
        if has_run:
            led = rman.ledger_check(con, run_id, through_day=ds)
            item["ledger"] = led["status"]
            if led["status"] != "PASS":
                state.alert(con, "CRITICAL", "LEDGER_INCONSISTENT", f"{scope} 纸面账本不一致：{led['problems'][:3]}",
                            scope=text, detail=led, now=now)
        ro = pointer.open_readonly(settings.release_db)
        try:
            m = rmon.run(settings, con, ro, scope, release_cfg, risk_cfg=risk_cfg, as_of_day=ds, now=now,
                         notify=lambda text_, s=text: state.alert(con, "CRITICAL", "MONITOR_FREEZE", text_, scope=s, now=now)
                         or "QUEUED_AS_ALERT")
        finally:
            if ro is not None:
                ro.close()
        item["monitor"] = m["status"]
        item["status"] = "DONE"
        scopes.append(item)
    manual = None
    account = cfg.account or manual_cfg.default_account_id
    if account:
        try:
            r = review.run(settings, con, None, None, ds, account_id=account, seal_first=True)
            manual = r.get("status", "OK") if isinstance(r, dict) else "OK"
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            manual = "NO_ACTIVITY" if code == "NO_ACTIVITY" else f"ERROR:{code or type(e).__name__}"
            if manual.startswith("ERROR"):
                state.alert(con, "WARN", "REVIEW_FAILED", f"人工轨 {account} {ds} 复盘失败：{e}", now=now)
    state.heartbeat(con, "daily", now, trading_day=ds)
    return {"status": "DONE", "trading_day": ds, "scopes": scopes, "manual_review": manual, "account": account}


def is_last_trading_day_of_week(d: date) -> bool:
    return sessions.is_trading_day(d) and sessions.next_trading_day(d).isocalendar()[1] != d.isocalendar()[1]


def run_weekly(settings: Settings, con: sqlite3.Connection, cfg: OpsConfig, *, registry, manual_cfg,
               day: str | None = None, now: int | None = None) -> dict:
    from ..evolve import run as evolve
    now = int(time.time()) if now is None else now
    d = _day(now, day)
    if not is_last_trading_day_of_week(d):
        return {"status": "NOT_LAST_TRADING_DAY", "trading_day": d.isoformat()}
    account = cfg.account or manual_cfg.default_account_id
    out = []
    for text in cfg.scopes:
        scope = parse_scope(text, settings)
        try:
            r = evolve.run(settings, con, scope, registry, account_id=account, now=now)
            created = r.get("created") or 0
            n_new = created if isinstance(created, int) else len(created)
            if n_new:
                state.alert(con, "INFO", "NEW_CANDIDATES", f"{scope} 新候选 {n_new} 个", scope=text,
                            detail={"candidates": [c.get("candidate_id") for c in r.get("candidates", [])]}, now=now)
            out.append({"scope": text, "status": r.get("result"), "new_candidates": n_new})
        except Exception as e:  # noqa: BLE001
            state.alert(con, "WARN", "EVOLVE_FAILED", f"{scope} 周度聚合失败：{e}", scope=text, now=now)
            out.append({"scope": text, "status": f"ERROR:{type(e).__name__}"})
    state.heartbeat(con, "weekly", now, trading_day=d.isoformat())
    return {"status": "DONE", "trading_day": d.isoformat(), "scopes": out}
