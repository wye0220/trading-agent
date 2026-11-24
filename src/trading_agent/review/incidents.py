"""事故草稿：DATA_ERROR、引擎异常和重大成交歧义。草稿只是立案，要等用户确认才进回归库（阶段 8）。

保存事故前的可恢复检查点和之后的原始事件。行情只记键和 revision（运行库本身保留全部 revision），
缺口原样列出，不补任何 K 线。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from ..config import Settings
from .rules import ENGINE_FLAGS, MAJOR_AMBIGUITY

LOOKBACK_SECONDS = 48 * 3600
EXPECTED = {
    "DATA_ERROR": ("数据完整、通过校验，策略正常运行", "数据校验没有通过，当时不产生新信号"),
    "ENGINE_ANOMALY": ("仓位在截止强平时按第一个可用价格平掉，成交后的保护单有效", "引擎给仓位或交易打了异常标记"),
    "MAJOR_FILL_AMBIGUITY": ("成交的先后和价格可以由行情唯一确定", "同一根子 K 内先后不明，或开盘直接越过止损，按保守规则处理"),
}


def triggers(f: dict) -> list[dict]:
    out = []
    errs = [e for e in f["events"] if e["signal_status"] == "DATA_ERROR"]
    if errs:
        out.append({"kind": "DATA_ERROR", "key": f["trading_day"], "at_utc": errs[0]["as_of_utc"],
                    "event_ids": [e["event_id"] for e in errs],
                    "detail": {"problems": sorted({p.get("category", "?") for e in errs for p in e["problems"]}),
                               "decisions": len(errs)}})
    day = f.get("day")
    if not errs and day is not None and f["events"] and (not day["complete"] or day["degraded"]):
        # 回放不做逐次数据校验：当日数据不完整或来源降级，同样按数据问题立案
        out.append({"kind": "DATA_ERROR", "key": f["trading_day"], "at_utc": f["events"][0]["as_of_utc"],
                    "event_ids": [f["events"][0]["event_id"]],
                    "detail": {"problems": [day["reason"] or ("DEGRADED" if day["degraded"] else "INCOMPLETE")],
                               "rth_present": day["rth_present"], "rth_expected": day["rth_expected"]}})
    for t in f["trades"]:
        for kind, names in (("ENGINE_ANOMALY", ENGINE_FLAGS), ("MAJOR_FILL_AMBIGUITY", MAJOR_AMBIGUITY)):
            hit = [x for x in t["flags"] if x in names]
            if hit:
                out.append({"kind": kind, "key": t["trade_id"], "at_utc": t["exit_time_utc"],
                            "event_ids": [t.get("event_id") or t["trade_id"]],
                            "detail": {"trade_id": t["trade_id"], "flags": hit, "exit_reason": t["exit_reason"],
                                       "entry_ticks": t["entry_ticks"], "exit_ticks": t["exit_ticks"]}})
    return out


def incident_id(run_id: str, kind: str, key: str) -> str:
    return "inc_" + hashlib.sha256(f"{run_id}|{kind}|{key}".encode()).hexdigest()[:16]


def _checkpoint(con: sqlite3.Connection, run_id: str, at_utc: int) -> dict | None:
    """事故前 48 小时内最早的一条决策记录：那一刻的策略状态、版本元组，以及当时的仓位和挂单。"""
    r = con.execute("SELECT event_id, as_of_utc, strategy_state_json, version_tuple_json, risk_domain, snapshot_id"
                    " FROM scan_events WHERE run_id=? AND as_of_utc>=? AND as_of_utc<=? ORDER BY as_of_utc LIMIT 1",
                    (run_id, at_utc - LOOKBACK_SECONDS, at_utc)).fetchone()
    if r is None:
        return None
    cp = r["as_of_utc"]
    positions = [dict(x) for x in con.execute(
        "SELECT p.position_id, p.side, p.quantity, p.entry_ticks, p.entry_time_utc, p.initial_stop_ticks, p.target_ticks"
        " FROM paper_positions p LEFT JOIN paper_trades t ON t.trade_id = p.position_id"
        " WHERE p.run_id=? AND p.entry_time_utc<=? AND (t.exit_time_utc IS NULL OR t.exit_time_utc>?)", (run_id, cp, cp))]
    orders = [dict(x) for x in con.execute(
        "SELECT order_id, side, order_type, trigger_ticks, limit_ticks, stop_ticks, armed_at_utc, expires_at_utc"
        " FROM paper_orders WHERE run_id=? AND armed_at_utc<=? AND expires_at_utc>?", (run_id, cp, cp))]
    return {"event_id": r["event_id"], "as_of_utc": cp, "watermark_utc": cp, "risk_domain": r["risk_domain"],
            "snapshot_id": r["snapshot_id"], "strategy_state": json.loads(r["strategy_state_json"]),
            "version_tuple": json.loads(r["version_tuple_json"]), "open_positions": positions, "working_orders": orders}


def build(settings: Settings, con: sqlite3.Connection, f: dict, trig: dict) -> dict:
    run_id = f["run_id"]
    iid = incident_id(run_id, trig["kind"], trig["key"])
    cp = _checkpoint(con, run_id, trig["at_utc"])
    start = cp["as_of_utc"] if cp else trig["at_utc"]
    end = f["rth_close_utc"] + 3600
    events = [dict(r) for r in con.execute(
        "SELECT event_id, as_of_utc, signal_status, risk_status, paper_action, snapshot_id FROM scan_events"
        " WHERE run_id=? AND as_of_utc>=? AND as_of_utc<? ORDER BY as_of_utc, event_id", (run_id, start, end))]
    day = f["day"] or {}
    revisions = []
    if day.get("source"):
        revisions = [list(r) for r in con.execute(
            "SELECT timeframe, bar_start_utc, revision, available_at_utc, derived FROM bars WHERE source=? AND contract_id=?"
            " AND bar_start_utc>=? AND bar_start_utc<? ORDER BY timeframe, bar_start_utc, revision",
            (day["source"], day["contract_id"], start - 300, end))]
    gaps = [dict(r) for r in con.execute(
        "SELECT category, start_utc, end_utc, detail FROM data_anomalies WHERE instrument_root=? AND start_utc>=?"
        " AND start_utc<? AND category IN ('UNEXPECTED_GAP','THIN_BAR','INCOMPLETE_HOUR','WRONG_CONTRACT')"
        " ORDER BY start_utc", (f["scope"].split(":")[1], start - 300, end))]
    exp, act = EXPECTED[trig["kind"]]
    return {"incident_id": iid, "status": "DRAFT", "kind": trig["kind"], "session_id": f["session_id"],
            "scope": f["scope"], "trading_day": f["trading_day"], "run_id": run_id, "run_mode": f["run_mode"],
            "incident_at_utc": trig["at_utc"], "evidence_event_ids": trig["event_ids"], "detail": trig["detail"],
            "expected_behavior": exp, "actual_behavior": act,
            "checkpoint": cp, "checkpoint_note": None if cp else "事故前 48 小时内没有决策记录，无法给出检查点",
            "raw_events": events, "market_revisions": {"source": day.get("source"), "contract_id": day.get("contract_id"),
                                                       "columns": ["timeframe", "bar_start_utc", "revision",
                                                                   "available_at_utc", "derived"], "rows": revisions},
            "gaps": gaps, "replay_verified": False,
            "note": "草稿。能否独立重放由评测阶段验证；重放不出来的要标 INCOMPLETE。"}


def save(settings: Settings, con: sqlite3.Connection, body: dict) -> dict:
    """写草稿。同一事故再次生成得到同一个标识，覆盖正文，不重复登记。"""
    path = settings.data_root / "incidents" / "drafts" / f"{body['incident_id']}.json"
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    now = int(time.time())
    summary = {"kind": body["kind"], "evidence_event_ids": body["evidence_event_ids"], "detail": body["detail"],
               "has_checkpoint": body["checkpoint"] is not None, "raw_events": len(body["raw_events"]),
               "gaps": len(body["gaps"])}
    with con:
        con.execute(
            "INSERT INTO incident_drafts (incident_id, session_id, scope, trading_day, kind, status, body_path,"
            " body_sha256, summary_json, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,'DRAFT',?,?,?,?,?)"
            " ON CONFLICT(incident_id) DO UPDATE SET body_sha256=excluded.body_sha256, summary_json=excluded.summary_json,"
            " updated_at_utc=excluded.updated_at_utc",
            (body["incident_id"], body["session_id"], body["scope"], body["trading_day"], body["kind"], str(path), sha,
             json.dumps(summary, ensure_ascii=False), now, now))
    return {"incident_id": body["incident_id"], "kind": body["kind"], "path": str(path), **summary}
