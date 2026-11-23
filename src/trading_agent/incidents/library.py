"""事故回归库：收录经用户确认的事故草稿，自带原始行情，能在临时库里独立重放。

条目 = 草稿（检查点、原始事件）+ 事故日及预热期的行情和交易日状态 + 期望结果。
重放从原始行情重新跑引擎，比对检查点时刻的策略状态、事故日的决策序列、订单和交易。重放不出来的标 INCOMPLETE。
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import sqlite3
import tempfile
import time
from datetime import date
from pathlib import Path

from .. import db, sessions
from ..config import Settings, parse_scope
from ..engine import runner
from ..engine.config import load_engine, load_risk

WARMUP_TRADING_DAYS = 20
CONFIRMED, INCOMPLETE = "CONFIRMED", "INCOMPLETE"
ORDER_COLS = ("side", "order_type", "status", "signal_bar_end_utc", "trigger_ticks", "limit_ticks", "stop_ticks", "end_reason")
TRADE_COLS = ("side", "quantity", "entry_ticks", "entry_time_utc", "exit_ticks", "exit_time_utc", "exit_reason",
              "pnl_ticks", "flags_json")


class IncidentError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _back(day: str, n: int) -> str:
    d = date.fromisoformat(day)
    for _ in range(n):
        d = sessions.prev_trading_day(d)
    return d.isoformat()


def _dump(con: sqlite3.Connection, root: str, start: str, end: str) -> dict:
    """事故重放需要的行情和交易日状态。"""
    q = {
        "bars": ("SELECT * FROM bars WHERE instrument_root=? AND trading_day>=? AND trading_day<=?", (root, start, end)),
        "trading_days": ("SELECT * FROM trading_days WHERE instrument_root=? AND trading_day>=? AND trading_day<=?",
                         (root, start, end)),
        "rth_days": ("SELECT r.* FROM rth_days r WHERE r.trading_day>=? AND r.trading_day<=? AND EXISTS (SELECT 1 FROM"
                     " bars b WHERE b.instrument_root=? AND b.source=r.source AND b.contract_id=r.contract_id"
                     " AND b.trading_day=r.trading_day)", (start, end, root)),
        "reference_levels": ("SELECT * FROM reference_levels WHERE instrument_root=? AND trading_day>=? AND trading_day<=?",
                             (root, start, end)),
        "day_quality": ("SELECT * FROM day_quality WHERE day>=? AND day<=?", (start, end)),
        "data_anomalies": ("SELECT * FROM data_anomalies WHERE instrument_root=? AND trading_day>=? AND trading_day<=?",
                           (root, start, end)),
    }
    out = {}
    for t, (sql, args) in q.items():
        cur = con.execute(sql, args)
        out[t] = {"columns": [c[0] for c in cur.description], "rows": [list(r) for r in cur.fetchall()]}
    return out


def _ledger(con: sqlite3.Connection, run_id: str, day: str, first_utc: int, last_utc: int) -> dict:
    orders = [list(r) for r in con.execute(
        f"SELECT {','.join(ORDER_COLS)} FROM paper_orders WHERE run_id=? AND signal_bar_end_utc>=? AND signal_bar_end_utc<=?"
        " ORDER BY signal_bar_end_utc, side, order_type", (run_id, first_utc - 600, last_utc))]
    trades = [list(r) for r in con.execute(
        f"SELECT {','.join(TRADE_COLS)} FROM paper_trades WHERE run_id=? AND trading_day=? ORDER BY entry_time_utc,"
        " exit_time_utc", (run_id, day))]
    return {"orders": orders, "trades": trades}


def _decisions(con: sqlite3.Connection, run_id: str, first_utc: int, last_utc: int) -> list[list]:
    return [list(r) for r in con.execute(
        "SELECT as_of_utc, signal_status, risk_status, paper_action FROM scan_events WHERE run_id=? AND as_of_utc>=?"
        " AND as_of_utc<=? ORDER BY as_of_utc, event_id", (run_id, first_utc, last_utc))]


def replay(settings: Settings, entry: dict, *, registry=None, risk_cfg=None, engine_cfg=None) -> dict:
    """在临时库里从原始行情重放，返回与期望的比对结果。"""
    from ..strategies.registry import default_registry
    exp = entry["expected"]
    if not exp["decisions"] or entry["draft"].get("checkpoint") is None:
        return {"reproduced": False, "reason": "NO_CHECKPOINT"}
    with tempfile.TemporaryDirectory(prefix="incident-replay-") as tmp:
        es = dataclasses.replace(settings, data_root=Path(tmp), runtime_db=Path(tmp) / "replay.sqlite",
                                 dev_visible_through=date.fromisoformat(entry["trading_day"]))
        scon = db.open_runtime(es)
        try:
            scon.execute("PRAGMA foreign_keys=OFF")
            with scon:
                for t, d in entry["data"].items():
                    if d["rows"]:
                        scon.executemany(f"INSERT INTO {t} ({','.join(d['columns'])}) VALUES ({','.join('?' * len(d['columns']))})",
                                         d["rows"])
            scope = parse_scope(entry["scope"], es)
            strategy = (registry or default_registry()).get(scope.strategy_family)
            try:
                runner.replay(es, scon, scope, strategy, date.fromisoformat(entry["data_start"]),
                              date.fromisoformat(entry["trading_day"]), run_id="incident",
                              risk_cfg=risk_cfg or load_risk(settings.project_root),
                              engine_cfg=engine_cfg or load_engine(settings.project_root), allow_real_data=True)
            except Exception as e:  # noqa: BLE001 — 重放跑不起来也是"重放不出来"
                return {"reproduced": False, "reason": f"REPLAY_ERROR:{type(e).__name__}"}
            first, last = exp["decisions"][0][0], exp["decisions"][-1][0]
            got = {"decisions": _decisions(scon, "incident", first, last),
                   **_ledger(scon, "incident", entry["trading_day"], first, last)}
            cp = entry["draft"]["checkpoint"]
            row = scon.execute("SELECT strategy_state_json FROM scan_events WHERE run_id='incident' AND as_of_utc=?",
                               (cp["as_of_utc"],)).fetchone()
            got["checkpoint_state"] = json.loads(row[0]) if row else None
        finally:
            scon.close()
    for key in ("checkpoint_state", "decisions", "orders", "trades"):
        if canon(got[key]) != canon(exp[key]):
            return {"reproduced": False, "reason": f"MISMATCH:{key}",
                    "counts": {k: len(got[k]) if isinstance(got[k], list) else None for k in ("decisions", "orders", "trades")}}
    return {"reproduced": True, "reason": None,
            "counts": {k: len(got[k]) for k in ("decisions", "orders", "trades")}}


def _path(settings: Settings, incident_id: str) -> Path:
    return settings.data_root / "incidents" / "library" / f"{incident_id}.json.gz"


def load(settings: Settings, con: sqlite3.Connection, incident_id: str) -> dict:
    r = con.execute("SELECT body_path, body_sha256 FROM incident_library WHERE incident_id=?", (incident_id,)).fetchone()
    if r is None:
        raise IncidentError("NOT_IN_LIBRARY", f"回归库里没有 {incident_id}")
    raw = Path(r["body_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != r["body_sha256"]:
        raise IncidentError("BODY_CHANGED", f"{incident_id} 的条目文件与登记的摘要不符")
    return json.loads(gzip.decompress(raw).decode("utf-8"))


def confirm(settings: Settings, con: sqlite3.Connection, incident_id: str, *, actor: str = "user:cli", registry=None,
            risk_cfg=None, engine_cfg=None, warmup_days: int = WARMUP_TRADING_DAYS, now: int | None = None,
            dry_run: bool = False) -> dict:
    """把一份你确认过的草稿收进回归库。收录时独立重放一次；重放不出来的标 INCOMPLETE，不算覆盖。

    dry_run：只试着重放，不收录、不改任何状态。
    """
    now = int(time.time()) if now is None else now
    if not actor.startswith("user:"):
        raise IncidentError("ACTOR_INVALID", "事故只能由用户确认")
    d = con.execute("SELECT * FROM incident_drafts WHERE incident_id=?", (incident_id,)).fetchone()
    if d is None:
        raise IncidentError("NO_DRAFT", f"没有这份事故草稿：{incident_id}")
    try:
        text = Path(d["body_path"]).read_text(encoding="utf-8")
    except OSError:
        raise IncidentError("NO_DRAFT", f"草稿文件不在了：{d['body_path']}") from None
    if hashlib.sha256(text.encode()).hexdigest() != d["body_sha256"]:
        raise IncidentError("BODY_CHANGED", f"草稿 {incident_id} 的文件与登记的摘要不符")
    draft = json.loads(text)
    root = draft["scope"].split(":")[1]
    start = _back(draft["trading_day"], warmup_days)
    events = draft["raw_events"]
    decisions = [[e["as_of_utc"], e["signal_status"], e["risk_status"], e["paper_action"]] for e in events]
    cp = draft.get("checkpoint")
    exp = {"decisions": decisions, "checkpoint_state": cp["strategy_state"] if cp else None, "orders": [], "trades": []}
    if decisions:
        exp.update(_ledger(con, draft["run_id"], draft["trading_day"], decisions[0][0], decisions[-1][0]))
    entry = {"incident_id": incident_id, "scope": draft["scope"], "trading_day": draft["trading_day"],
             "kind": draft["kind"], "data_start": start, "warmup_trading_days": warmup_days,
             "draft": {**draft, "status": "CONFIRMED"}, "expected": exp,
             "data": _dump(con, root, start, draft["trading_day"])}
    res = replay(settings, entry, registry=registry, risk_cfg=risk_cfg, engine_cfg=engine_cfg)
    status = CONFIRMED if res["reproduced"] else INCOMPLETE
    if dry_run:
        return {"incident_id": incident_id, "status": "DRY_RUN", "would_be": status, "kind": draft["kind"],
                "trading_day": draft["trading_day"], "replay": res, "bars": len(entry["data"]["bars"]["rows"])}
    entry["status"] = status
    raw = gzip.compress(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8"), mtime=0)
    p = _path(settings, incident_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    with con:
        con.execute(
            "INSERT INTO incident_library (incident_id, scope, trading_day, kind, status, body_path, body_sha256,"
            " expect_sha256, confirmed_by, note, last_replay_json, created_at_utc, updated_at_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET status=excluded.status,"
            " body_sha256=excluded.body_sha256, expect_sha256=excluded.expect_sha256, note=excluded.note,"
            " last_replay_json=excluded.last_replay_json, updated_at_utc=excluded.updated_at_utc",
            (incident_id, draft["scope"], draft["trading_day"], draft["kind"], status, str(p),
             hashlib.sha256(raw).hexdigest(), hashlib.sha256(canon(exp).encode()).hexdigest(), actor, res["reason"],
             canon(res), now, now))
        con.execute("UPDATE incident_drafts SET status=?, updated_at_utc=? WHERE incident_id=?", (status, now, incident_id))
    return {"incident_id": incident_id, "status": status, "kind": draft["kind"], "trading_day": draft["trading_day"],
            "replay": res, "path": str(p), "bars": len(entry["data"]["bars"]["rows"])}


def listing(con: sqlite3.Connection) -> dict:
    lib = [{"incident_id": r["incident_id"], "kind": r["kind"], "trading_day": r["trading_day"], "scope": r["scope"],
            "status": r["status"], "note": r["note"]} for r in con.execute(
        "SELECT * FROM incident_library ORDER BY trading_day, incident_id")]
    drafts = [{"incident_id": r["incident_id"], "kind": r["kind"], "trading_day": r["trading_day"], "scope": r["scope"]}
              for r in con.execute("SELECT * FROM incident_drafts WHERE status='DRAFT' ORDER BY trading_day, incident_id")]
    return {"library": lib, "drafts": drafts}


def run_all(settings: Settings, con: sqlite3.Connection, *, registry=None, risk_cfg=None, engine_cfg=None) -> dict:
    """重放全部 CONFIRMED 条目。任一对不上就是回归失败。INCOMPLETE 的单列，不算覆盖。"""
    rows = con.execute("SELECT incident_id, status FROM incident_library ORDER BY trading_day, incident_id").fetchall()
    failed, passed = [], 0
    for r in rows:
        if r["status"] != CONFIRMED:
            continue
        try:
            res = replay(settings, load(settings, con, r["incident_id"]), registry=registry, risk_cfg=risk_cfg,
                         engine_cfg=engine_cfg)
        except IncidentError as e:
            res = {"reproduced": False, "reason": e.reason}
        with con:
            con.execute("UPDATE incident_library SET last_replay_json=?, updated_at_utc=? WHERE incident_id=?",
                        (canon(res), int(time.time()), r["incident_id"]))
        if res["reproduced"]:
            passed += 1
        else:
            failed.append({"incident_id": r["incident_id"], "reason": res["reason"]})
    incomplete = sum(r["status"] == INCOMPLETE for r in rows)
    return {"ok": not failed, "confirmed": passed + len(failed), "passed": passed, "failed": failed,
            "incomplete": incomplete,
            "summary": f"{passed + len(failed)} 个条目，通过 {passed}，失败 {len(failed)}，另有 {incomplete} 个 INCOMPLETE 不算覆盖"}
