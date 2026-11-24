"""读出一个已封存 session 的全部事实：决策、订单、交易、数据异常，以及（可选的）人工轨记录。只读。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from .. import sessions
from ..config import Scope, Settings
from ..engine.config import RiskConfig
from ..manual import discipline
from ..manual.config import ManualConfig
from ..research import metrics
from .seal import day_window


def load(settings: Settings, con: sqlite3.Connection, scope: Scope, session: sqlite3.Row | dict, risk_cfg: RiskConfig,
         *, account_id: str | None = None, manual_cfg: ManualConfig | None = None) -> dict:
    run_id, ds = session["run_id"], session["trading_day"]
    day = date.fromisoformat(ds)
    lo, hi = day_window(day)
    rth_open, rth_close = sessions.rth_bounds(day)
    events, meta = [], {}
    for r in con.execute("SELECT event_id, as_of_utc, signal_status, risk_status, paper_action, decision_json,"
                         " version_tuple_json FROM scan_events WHERE run_id=? AND as_of_utc>=? AND as_of_utc<?"
                         " ORDER BY as_of_utc, event_id", (run_id, lo, hi)):
        d = json.loads(r["decision_json"])
        m = d.get("decision_metadata") or {}
        e = {"event_id": r["event_id"], "as_of_utc": r["as_of_utc"], "signal_status": r["signal_status"],
             "risk_status": r["risk_status"], "paper_action": r["paper_action"], "block_reason": d.get("block_reason"),
             "order_id": d.get("order_id"), "skip_reason": m.get("skip_reason"), "setup_id": m.get("setup_id"),
             "sub_bar_timeframe": d.get("sub_bar_timeframe"), "metadata": m,
             "problems": d.get("problems") or [], "version_tuple": json.loads(r["version_tuple_json"])}
        events.append(e)
        if r["signal_status"] == "SETUP_VALID" and m.get("setup_id"):
            meta[m["setup_id"]] = {**m, "_event_id": r["event_id"]}
    orders = [dict(r) for r in con.execute(
        "SELECT order_id, setup_id, side, order_type, status, end_reason, signal_bar_end_utc, armed_at_utc,"
        " expires_at_utc, trigger_ticks, limit_ticks, stop_ticks, flags_json FROM paper_orders WHERE run_id=?"
        " AND signal_bar_end_utc>=? AND signal_bar_end_utc<? ORDER BY signal_bar_end_utc, order_id", (run_id, lo, hi))]
    trades = metrics.load_trades(settings, con, scope, run_id, risk_cfg, meta, trading_day=ds)
    targets = dict(con.execute("SELECT position_id, target_ticks FROM paper_positions WHERE run_id=? AND trading_day=?",
                               (run_id, ds)).fetchall())
    for t in trades:
        t["target_ticks"] = targets.get(t["trade_id"])
        t["event_id"] = (meta.get(t["setup_id"] or "") or {}).get("_event_id")
    td = con.execute("SELECT * FROM trading_days WHERE instrument_root=? AND trading_day=?",
                     (scope.instrument_root, ds)).fetchone()
    bars = []
    if td is not None and td["source"]:
        bars = [tuple(r) for r in con.execute(
            "SELECT bar_start_utc, bar_end_utc, high, low FROM bars b WHERE source=? AND contract_id=? AND timeframe=?"
            " AND trading_day=? AND session_type='RTH' AND revision=(SELECT MAX(revision) FROM bars x WHERE"
            " x.source=b.source AND x.contract_id=b.contract_id AND x.timeframe=b.timeframe"
            " AND x.bar_start_utc=b.bar_start_utc) ORDER BY bar_start_utc",
            (td["source"], td["contract_id"], scope.timeframe, ds))]
    anomalies = [dict(r) for r in con.execute(
        "SELECT anomaly_id, category, start_utc, end_utc, detail FROM data_anomalies WHERE instrument_root=?"
        " AND trading_day=? ORDER BY start_utc, anomaly_id", (scope.instrument_root, ds))]
    # 引擎异常以仓位和交易上的标记为准（告警的写入时刻是墙上时间，回放时与交易日对不上）
    out = {"session_id": session["session_id"], "scope": str(scope), "trading_day": ds, "run_id": run_id,
           "run_mode": session["run_mode"], "rth_open_utc": rth_open, "rth_close_utc": rth_close,
           "tick_size": str(settings.instrument(scope.instrument_root).tick_size),
           "events": events, "orders": orders, "trades": trades, "bars": bars, "anomalies": anomalies,
           "day": None if td is None else {k: td[k] for k in ("contract_id", "source", "complete", "degraded", "reason",
                                                               "rth_expected", "rth_present", "gap_count", "fixture")},
           "manual": None}
    if account_id:
        out["manual"] = _manual(con, account_id, ds, manual_cfg, rth_close)
    return out


def load_manual_only(con: sqlite3.Connection, session: sqlite3.Row | dict, account_id: str,
                     manual_cfg: ManualConfig | None) -> dict:
    """只有人工轨的 session：没有纸面运行，也不读任何行情。"""
    ds = session["trading_day"]
    rth_open, rth_close = sessions.rth_bounds(date.fromisoformat(ds))
    return {"session_id": session["session_id"], "scope": session["scope"], "trading_day": ds, "run_id": None,
            "run_mode": session["run_mode"], "rth_open_utc": rth_open, "rth_close_utc": rth_close, "tick_size": "0.01",
            "events": [], "orders": [], "trades": [], "bars": [], "anomalies": [], "day": None,
            "manual": _manual(con, account_id, ds, manual_cfg, rth_close)}


def _manual(con, account_id: str, ds: str, cfg: ManualConfig | None, session_end: int) -> dict:
    rows = lambda sql, *a: [dict(r) for r in con.execute(sql, a)]       # noqa: E731
    trades = rows("SELECT * FROM manual_trades WHERE account_id=? AND trading_day=? ORDER BY executed_at_utc, trade_id",
                  account_id, ds)
    devents = rows("SELECT * FROM discipline_events WHERE account_id=? AND trading_day=? ORDER BY action_time_utc,"
                   " event_id", account_id, ds)
    alerts = rows("SELECT * FROM discipline_alerts WHERE account_id=? AND trading_day=? ORDER BY created_at_utc,"
                  " alert_id", account_id, ds)
    plans = rows("SELECT plan_id, contract_id, side, status, entry_ticks, stop_ticks, target_ticks, quantity,"
                 " submitted_at_utc, activated_at_utc FROM trader_plans WHERE account_id=? AND submitted_at_utc>=?"
                 " AND submitted_at_utc<? ORDER BY submitted_at_utc", account_id, session_end - 86400, session_end + 3600)
    ids = {t["trade_id"] for t in trades} | {e["event_id"] for e in devents} | {a["alert_id"] for a in alerts}
    notes = [n for n in rows("SELECT * FROM trader_notes ORDER BY created_at_utc, note_id") if n["event_id"] in ids]
    return {"account_id": account_id, "trades": trades, "discipline_events": devents, "alerts": alerts, "plans": plans,
            "notes": notes, "ids": sorted(ids),
            "kpis": None if cfg is None else discipline.session_kpis(con, cfg, account_id, ds, session_end)}
