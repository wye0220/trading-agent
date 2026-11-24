"""session 的封存：一个运行的一个交易日，在截止强平之后固定下来，写入当日指标。只有封存的 session 才能复盘。"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from datetime import date
from decimal import Decimal

from .. import sessions
from ..config import Scope, Settings
from ..engine.config import EngineConfig, load_engine

SEALED = "SEALED"


class SealError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def session_id(run_id: str, trading_day: str) -> str:
    return f"{run_id}:{trading_day}"


def day_window(day: date) -> tuple[int, int]:
    """该交易日的决策记录所在的时间范围：RTH 开盘到收盘后一小时（含可用时点的延迟）。"""
    o, c = sessions.rth_bounds(day)
    return o, c + 3600


def get(con: sqlite3.Connection, sid: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()


def kpis(con: sqlite3.Connection, run_id: str, day: date) -> dict:
    lo, hi = day_window(day)
    ds = day.isoformat()
    # 按时间窗口，或按决策所属的交易日（盘后补扫的决策时点晚于窗口，但属于这一天）
    within = "run_id=? AND ((as_of_utc>=? AND as_of_utc<?) OR json_extract(decision_json,'$.trading_day')=?)"
    ev = con.execute("SELECT COUNT(*), SUM(signal_status='SETUP_VALID'), SUM(signal_status='DATA_ERROR'),"
                     " MAX(as_of_utc) FROM scan_events WHERE " + within, (run_id, lo, hi, ds)).fetchone()
    last = con.execute("SELECT version_tuple_json FROM scan_events WHERE " + within + " ORDER BY as_of_utc DESC LIMIT 1",
                       (run_id, lo, hi, ds)).fetchone()
    orders = dict(con.execute("SELECT status, COUNT(*) FROM paper_orders WHERE run_id=? AND signal_bar_end_utc>=?"
                              " AND signal_bar_end_utc<? GROUP BY 1", (run_id, lo, hi)).fetchall())
    net, r, flags, n = Decimal(0), Decimal(0), Counter(), 0
    has_r = True
    for t in con.execute("SELECT net_usd, r_research, flags_json FROM paper_trades WHERE run_id=? AND trading_day=?",
                         (run_id, ds)):
        n += 1
        net += Decimal(t["net_usd"])
        if t["r_research"] is None:
            has_r = False
        else:
            r += Decimal(t["r_research"])
        flags.update(json.loads(t["flags_json"]))
    return {"decisions": ev[0] or 0, "opportunities": ev[1] or 0, "data_errors": ev[2] or 0,
            "orders": sum(orders.values()), "orders_by_status": orders, "trades": n, "net_usd": str(net),
            "net_r": str(r) if has_r else None, "flags": dict(flags), "last_decision_utc": ev[3],
            "version_tuple": json.loads(last[0]) if last else {}}


def seal(settings: Settings, con: sqlite3.Connection, scope: Scope, run_id: str, day: date,
         engine_cfg: EngineConfig | None = None) -> dict:
    """封存。已封存的直接返回原记录（幂等，指标不改）。条件不满足时抛 SealError。"""
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    ds = day.isoformat()
    sid = session_id(run_id, ds)
    row = get(con, sid)
    if row is not None and row["status"] == SEALED:
        return {**dict(row), "kpi": json.loads(row["kpi_json"]), "created": False}
    run = con.execute("SELECT * FROM engine_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise SealError("NO_RUN", f"没有这个运行：{run_id}")
    if run["scope"] != str(scope):
        raise SealError("SCOPE_MISMATCH", f"运行 {run_id} 属于 {run['scope']}，不是 {scope}")
    if not sessions.is_trading_day(day):
        raise SealError("NOT_TRADING_DAY", f"{ds} 不是交易日")
    td = con.execute("SELECT fixture, complete, degraded, source FROM trading_days WHERE instrument_root=?"
                     " AND trading_day=?", (scope.instrument_root, ds)).fetchone()
    fixture = bool(td["fixture"]) if td is not None else False
    cut = settings.dev_visible_through
    if td is None and kpis(con, run_id, day)["decisions"] == 0:
        raise SealError("NO_ACTIVITY", f"运行 {run_id} 在 {ds} 没有任何决策记录")
    from .. import live_window
    live = cut is not None and live_window.effective(con, settings.project_root).is_live_day(scope.instrument_root, ds)
    if not fixture and (cut is None or day > cut) and not live:
        # 保留窗口和未解封的胶囊由评测侧处理，不能流回普通复盘。放行的实时行情日除外（2026-11-23 用户决定）：
        # 它们本来就给纸面运行看，聚合仍按截止日把这些 session 排除在外。
        raise SealError("HOLDOUT_PROTECTED", f"{ds} 晚于开发可见截止日（或截止日没有设置），不能封存为普通 session")
    k = kpis(con, run_id, day)
    if k["decisions"] == 0:
        raise SealError("NO_ACTIVITY", f"运行 {run_id} 在 {ds} 没有任何决策记录")
    _, close = sessions.rth_bounds(day)
    flatten = close - engine_cfg.flatten_minutes_before_close * 60
    if run["watermark_utc"] < flatten:
        raise SealError("SESSION_IN_PROGRESS", f"{ds} 还没有过截止强平时刻")
    if con.execute("SELECT 1 FROM paper_positions WHERE run_id=? AND trading_day=? AND status='OPEN' LIMIT 1",
                   (run_id, ds)).fetchone():
        raise SealError("POSITION_STILL_OPEN", f"{ds} 还有未平的纸面仓位")
    lo, hi = day_window(day)
    if con.execute("SELECT 1 FROM paper_orders WHERE run_id=? AND signal_bar_end_utc>=? AND signal_bar_end_utc<?"
                   " AND status IN ('ARMED','TRIGGERED') LIMIT 1", (run_id, lo, hi)).fetchone():
        raise SealError("SESSION_IN_PROGRESS", f"{ds} 还有工作中的挂单")
    k["data"] = None if td is None else {"complete": bool(td["complete"]), "degraded": bool(td["degraded"]),
                                         "source": td["source"], "fixture": fixture}
    now = int(time.time())
    with con:
        con.execute(
            "INSERT INTO sessions (session_id, scope, trading_day, run_mode, risk_domain, run_id, status, kpi_json,"
            " sealed_at_utc, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET status=excluded.status, kpi_json=excluded.kpi_json,"
            " sealed_at_utc=excluded.sealed_at_utc, version=version+1",
            (sid, str(scope), ds, run["run_mode"], run["risk_domain"], run_id, SEALED,
             json.dumps(k, ensure_ascii=False), now, now))
    return {**dict(get(con, sid)), "kpi": k, "created": True}


# ---------------------------------------------------------------- 人工轨的 session
def manual_session_id(account_id: str, trading_day: str) -> str:
    return f"manual:{account_id}:{trading_day}"


def seal_manual(settings: Settings, con: sqlite3.Connection, account_id: str, day: date, manual_cfg,
                now_utc: int | None = None) -> dict:
    """封存一个账户一个交易日的人工轨 session。它只含你自己的计划、成交和提醒，不含任何行情，
    所以不受开发可见截止日的限制。当日 Globex 时段结束（17:00 ET）之后才能封存。"""
    from ..manual import discipline
    ds = day.isoformat()
    sid = manual_session_id(account_id, ds)
    row = get(con, sid)
    if row is not None and row["status"] == SEALED:
        return {**dict(row), "kpi": json.loads(row["kpi_json"]), "created": False}
    now = int(time.time()) if now_utc is None else now_utc
    if not sessions.is_trading_day(day):
        raise SealError("NOT_TRADING_DAY", f"{ds} 不是交易日")
    # Globex 时段到当日 17:00 ET 结束（17:00–18:00 是维护时段）。盘后任务 17:45 运行，不能等到 18:00 的交易日分界。
    # 指标只统计到 RTH 收盘，所以 17:00 之后封存与 18:00 之后封存内容相同。
    if now < sessions.eth_bounds(day)[1]:
        raise SealError("SESSION_IN_PROGRESS", f"{ds} 这个交易日还没有结束（17:00 ET 之后才能封存）")
    k = discipline.session_kpis(con, manual_cfg, account_id, ds, sessions.rth_bounds(day)[1])
    if not k["opens"] and not k["closes"] and not k["alerts"]:
        raise SealError("NO_ACTIVITY", f"账户 {account_id} 在 {ds} 没有成交，也没有提醒")
    with con:
        con.execute(
            "INSERT INTO sessions (session_id, scope, trading_day, run_mode, risk_domain, run_id, status, kpi_json,"
            " sealed_at_utc, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET status=excluded.status, kpi_json=excluded.kpi_json,"
            " sealed_at_utc=excluded.sealed_at_utc, scope=excluded.scope, version=version+1",
            (sid, f"manual:{account_id}", ds, "manual_actual", f"account:{account_id}", sid, SEALED,
             json.dumps(k, ensure_ascii=False), now, now))
    return {**dict(get(con, sid)), "kpi": k, "created": True}
