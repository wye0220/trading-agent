"""纪律事件、交易员纠正、人工轨 KPI。

plan_adherence 比较真实动作与当时生效的个人计划；strategy_alignment 取自计划预检时对策略的比较。两者独立。
证据不足时标 UNKNOWN，不推断。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from statistics import median

from ..config import Settings
from .config import RULES, ManualConfig

TYPES = ("COUNTERTREND_BEFORE_CONFIRMATION", "RETRY_WITHOUT_NEW_SETUP", "EARLY_EXIT", "STOP_WIDENED", "STOP_DELAYED",
         "UNPLANNED", "MISSING_EVIDENCE")


def _emit(con, trade: sqlite3.Row, type_: str, adherence: str, alignment: str, plan_id: str | None, **evidence) -> str:
    eid = "de_" + hashlib.sha256(f"{trade['trade_id']}|{type_}".encode()).hexdigest()[:24]
    con.execute("INSERT OR IGNORE INTO discipline_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (eid, trade["account_id"], trade["trading_day"], type_, plan_id, trade["trade_id"],
                 trade["executed_at_utc"], adherence, alignment, json.dumps(evidence, ensure_ascii=False),
                 int(time.time())))
    return eid


def _plan(con, plan_id: str | None):
    if not plan_id:
        return None, {}
    p = con.execute("SELECT * FROM trader_plans WHERE plan_id=?", (plan_id,)).fetchone()
    return p, (json.loads(p["pretrade_json"] or "{}").get("check", {}) if p else {})


def on_open(con: sqlite3.Connection, settings: Settings, cfg: ManualConfig, *, trade_id: str) -> list[str]:
    """一笔开仓成交入库后生成纪律事件。调用方负责事务。"""
    t = con.execute("SELECT * FROM manual_trades WHERE trade_id=?", (trade_id,)).fetchone()
    out = []
    if t["plan_link_status"] == "UNPLANNED":
        out.append(_emit(con, t, "UNPLANNED", "VIOLATION", "UNKNOWN", None, note=t["link_note"]))
    plan, check = _plan(con, t["plan_id"] if t["plan_link_status"] == "LINKED" else None)
    alignment = check.get("strategy_alignment", "UNKNOWN")
    flags = check.get("discipline_flags", [])
    for f in ("COUNTERTREND_BEFORE_CONFIRMATION", "RETRY_WITHOUT_NEW_SETUP"):
        if f in flags:                                # 计划里就带着这个标记：按计划做了，但偏离策略
            out.append(_emit(con, t, f, "COMPLIANT", alignment, plan["plan_id"], source="pretrade_check"))
    if plan is None and "RETRY_WITHOUT_NEW_SETUP" not in flags:
        prev = con.execute(
            "SELECT * FROM manual_trades WHERE account_id=? AND contract_id=? AND effect='CLOSE' AND quarantined=0"
            " AND executed_at_utc<=? ORDER BY executed_at_utc DESC, trade_id DESC LIMIT 1",
            (t["account_id"], t["contract_id"], t["executed_at_utc"])).fetchone()
        if (prev is not None and prev["exit_kind"] == "STOP" and prev["side"] == t["side"]
                and t["executed_at_utc"] - prev["executed_at_utc"] <= cfg.cooldown_minutes * 60):
            out.append(_emit(con, t, "RETRY_WITHOUT_NEW_SETUP", "VIOLATION", "UNKNOWN", None,
                             previous_stop_trade_id=prev["trade_id"],
                             seconds_since_stop=t["executed_at_utc"] - prev["executed_at_utc"]))
    return out


def _stop_widened(con, t: sqlite3.Row, opened_at_utc: int | None) -> list[dict]:
    """持仓期间止损单是否被改到离入场更远。只看带时间和价格的订单事件。"""
    if opened_at_utc is None:
        return []
    d = 1 if t["side"] == "LONG" else -1
    last: dict[str, int] = {}
    found = []
    for e in con.execute(
            "SELECT broker_order_id, event_type, event_time_utc, price_ticks FROM manual_order_events"
            " WHERE account_id=? AND contract_id=? AND order_kind IN ('STOP','STOP_LIMIT') AND price_ticks IS NOT NULL"
            " AND event_time_utc<=? ORDER BY event_time_utc, event_id",
            (t["account_id"], t["contract_id"], t["executed_at_utc"])):
        oid, px = e["broker_order_id"], e["price_ticks"]
        if (oid in last and e["event_type"] == "MODIFIED" and e["event_time_utc"] >= opened_at_utc
                and d * (px - last[oid]) < 0):       # 多仓的止损被下移 / 空仓的止损被上移
            found.append({"broker_order_id": oid, "from_ticks": last[oid], "to_ticks": px,
                          "at_utc": e["event_time_utc"]})
        last[oid] = px
    return found


def on_close(con: sqlite3.Connection, settings: Settings, cfg: ManualConfig, *, trade_id: str,
             opened_at_utc: int | None) -> list[str]:
    """一笔平仓成交入库后判定计划执行情况。调用方负责事务。"""
    t = con.execute("SELECT * FROM manual_trades WHERE trade_id=?", (trade_id,)).fetchone()
    plan, check = _plan(con, t["plan_id"])
    alignment = check.get("strategy_alignment", "UNKNOWN")
    out, adherence = [], "UNKNOWN"
    if plan is not None:
        d = 1 if t["side"] == "LONG" else -1
        px, stop, target = t["price_ticks"], plan["stop_ticks"], plan["target_ticks"]
        has_events = con.execute("SELECT 1 FROM manual_order_events WHERE account_id=? AND broker_order_id=? LIMIT 1",
                                 (t["account_id"], t["broker_order_id"] or "")).fetchone() is not None
        if not has_events:
            out.append(_emit(con, t, "MISSING_EVIDENCE", "UNKNOWN", alignment, plan["plan_id"],
                             why="这笔平仓没有订单生命周期事件，无法判定提前离场、放宽止损或延迟止损",
                             exit_ticks=px, plan_stop_ticks=stop, plan_target_ticks=target))
        else:
            violations = 0
            for w in _stop_widened(con, t, opened_at_utc):
                out.append(_emit(con, t, "STOP_WIDENED", "VIOLATION", alignment, plan["plan_id"], **w))
                violations += 1
                break
            kind = t["exit_kind"]
            if kind != "STOP":                        # 止损单成交（哪怕跳空成交得更差）是按计划止损
                at_target = target is not None and d * (px - target) >= 0
                if stop is not None and d * (stop - px) > cfg.stop_tolerance_ticks:
                    out.append(_emit(con, t, "STOP_DELAYED", "VIOLATION", alignment, plan["plan_id"], exit_kind=kind,
                                     exit_ticks=px, plan_stop_ticks=stop, worse_by_ticks=d * (stop - px)))
                    violations += 1
                elif (stop is not None and target is not None and d * (px - stop) > 0 and not at_target):
                    out.append(_emit(con, t, "EARLY_EXIT", "VIOLATION", alignment, plan["plan_id"], exit_kind=kind,
                                     exit_ticks=px, plan_stop_ticks=stop, plan_target_ticks=target))
                    violations += 1
            adherence = "VIOLATION" if violations else "COMPLIANT"
    con.execute("UPDATE manual_trades SET plan_adherence=?, strategy_alignment=? WHERE trade_id=?",
                (adherence, alignment, trade_id))
    return out


# ---------------------------------------------------------------- 交易员纠正
def add_note(con: sqlite3.Connection, event_id: str | None, body: str, author: str) -> str:
    """保存交易员对某个事件的纠正。正文是数据，不是指令；它不改变任何规则、状态或已有的记录。"""
    if not body or not body.strip():
        raise ValueError("纠正的正文不能为空")
    nid = "note_" + uuid.uuid4().hex[:16]
    with con:
        con.execute("INSERT INTO trader_notes VALUES (?,?,?,?,?)", (nid, event_id, body, author, int(time.time())))
    return nid


# ---------------------------------------------------------------- KPI
def session_kpis(con: sqlite3.Connection, cfg: ManualConfig, account_id: str, trading_day: str,
                 session_end_utc: int | None = None) -> dict:
    """一个账户一个交易日的人工轨指标。隔离的成交不计。"""
    opens = con.execute(
        "SELECT plan_link_status, COUNT(*) FROM manual_trades WHERE account_id=? AND trading_day=? AND effect='OPEN'"
        " AND quarantined=0 GROUP BY 1", (account_id, trading_day)).fetchall()
    by = {r[0]: r[1] for r in opens}
    total = sum(by.values())
    coverage = None if total == 0 else by.get("LINKED", 0) / total
    closes = dict(con.execute(
        "SELECT COALESCE(plan_adherence,'UNKNOWN'), COUNT(*) FROM manual_trades WHERE account_id=? AND trading_day=?"
        " AND effect='CLOSE' AND quarantined=0 GROUP BY 1", (account_id, trading_day)).fetchall())
    events = dict(con.execute("SELECT type, COUNT(*) FROM discipline_events WHERE account_id=? AND trading_day=?"
                              " GROUP BY 1", (account_id, trading_day)).fetchall())
    alerts = {}
    for rule in RULES:
        rows = con.execute("SELECT created_at_utc, resolved_at_utc FROM discipline_alerts WHERE account_id=? AND"
                           " trading_day=? AND rule=?", (account_id, trading_day, rule)).fetchall()
        if not rows:
            continue
        handled = [r[1] - r[0] for r in rows if r[1] is not None
                   and (session_end_utc is None or r[1] <= session_end_utc)]
        open_n = len(rows) - len(handled)
        alerts[rule] = {"count": len(rows), "resolved": len(handled),
                        "median_seconds_to_resolve": None if not handled else median(handled),
                        "unresolved_share": open_n / len(rows)}
    decidable = closes.get("COMPLIANT", 0) + closes.get("VIOLATION", 0)
    return {
        "account_id": account_id, "trading_day": trading_day,
        "opens": total, "opens_planned": by.get("LINKED", 0), "opens_unplanned": by.get("UNPLANNED", 0),
        "opens_link_unknown": by.get("UNKNOWN", 0), "plan_coverage": coverage,
        "plan_coverage_threshold": cfg.plan_coverage_threshold,
        "sample_insufficient": coverage is not None and coverage < cfg.plan_coverage_threshold,
        "closes": sum(closes.values()), "adherence_decidable": decidable,
        "adherence_unknown": closes.get("UNKNOWN", 0), "adherence_violations": closes.get("VIOLATION", 0),
        "discipline_events": {t: events.get(t, 0) for t in TYPES if events.get(t)},
        "alerts": alerts,
        "quarantined_trades": con.execute("SELECT COUNT(*) FROM manual_trades WHERE account_id=? AND trading_day=?"
                                          " AND quarantined=1", (account_id, trading_day)).fetchone()[0],
    }


def write_session_kpis(con: sqlite3.Connection, cfg: ManualConfig, account_id: str, trading_day: str,
                       session_end_utc: int | None = None) -> dict:
    k = session_kpis(con, cfg, account_id, trading_day, session_end_utc)
    sid = f"manual:{account_id}:{trading_day}"
    now = int(time.time())
    with con:
        con.execute(
            "INSERT INTO sessions (session_id, scope, trading_day, run_mode, risk_domain, run_id, status, kpi_json,"
            " created_at_utc) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET kpi_json=excluded.kpi_json,"
            " version=version+1", (sid, "manual", trading_day, "manual_actual", f"account:{account_id}", sid, "OPEN",
                                   json.dumps(k, ensure_ascii=False), now))
    return k
