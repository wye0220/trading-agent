"""人工成交与订单事件的只读导入：去重、开平判定、计划关联、纪律事件。

manual_trades 的一行是一笔成交的"开仓部分"或"平仓部分"；side 记的是仓位方向（LONG / SHORT）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .. import contracts, sessions
from ..bars import OffTickPrice, decimal_to_ticks
from ..config import Settings
from ..engine import risk as risk_mod
from ..engine.config import RiskConfig
from . import discipline, plans
from .config import ManualConfig

TRADE_COLUMNS = ("account_id", "contract", "side", "quantity", "executed_at_utc", "price", "source")
EVENT_COLUMNS = ("account_id", "broker_order_id", "event_type", "event_time_utc", "source")
EVENT_TYPES = ("SUBMITTED", "MODIFIED", "CANCELLED", "TRIGGERED", "FILLED", "REJECTED", "EXPIRED")
ORDER_KINDS = ("STOP", "STOP_LIMIT", "LIMIT", "MARKET")
BUY = {"BUY": 1, "B": 1, "LONG": 1, "SELL": -1, "S": -1, "SHORT": -1}


class ImportError_(ValueError):
    """文件整体被拒收。"""


def _h(*parts) -> str:
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def _read(path: Path, required: tuple[str, ...]) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        header = [h.strip() for h in (rd.fieldnames or [])]
        missing = [c for c in required if c not in header]
        if missing:
            raise ImportError_("缺少必需列：" + "、".join(missing))
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in rd]


def _contract(text: str, at_utc: int, settings: Settings):
    cid = contracts.normalize(text.upper(), datetime.fromtimestamp(at_utc, timezone.utc).date())
    root = contracts.parse(cid)[0]
    return cid, settings.instrument(root)


def _day(at_utc: int) -> str:
    return sessions.trading_day_at(at_utc).isoformat()


# ---------------------------------------------------------------- 订单事件
def import_order_events(settings: Settings, con: sqlite3.Connection, path: Path) -> dict:
    """导入订单生命周期事件。可选列：contract、side、order_kind、price、quantity。按内容幂等。"""
    rows = _read(Path(path), EVENT_COLUMNS)
    counts = Counter()
    now = int(time.time())
    with con:
        for i, r in enumerate(rows):
            try:
                et = r["event_type"].upper()
                kind = (r.get("order_kind") or "").upper() or None
                if et not in EVENT_TYPES or (kind and kind not in ORDER_KINDS):
                    raise ValueError(f"未知的事件类型或订单类型：{et} / {kind}")
                at = sessions.parse_utc(r["event_time_utc"])
                cid, price = None, None
                if r.get("contract"):
                    cid, inst = _contract(r["contract"], at, settings)
                    if r.get("price"):
                        price = decimal_to_ticks(r["price"], inst.tick_size)
                side = None if not r.get("side") else ("LONG" if BUY[r["side"].upper()] > 0 else "SHORT")
                qty = int(r["quantity"]) if r.get("quantity") else None
            except (ValueError, KeyError, OffTickPrice, contracts.ContractError) as e:
                counts["bad_rows"] += 1
                counts.setdefault("first_error", f"第 {i + 2} 行：{e}")   # type: ignore[arg-type]
                continue
            eid = "oe_" + _h(r["source"], r["account_id"], r["broker_order_id"], et, at, kind, price, qty)[:24]
            cur = con.execute(
                "INSERT OR IGNORE INTO manual_order_events (event_id, account_id, broker_order_id, event_type,"
                " event_time_utc, payload_json, source, imported_at_utc, contract_id, side, order_kind, price_ticks,"
                " quantity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, r["account_id"], r["broker_order_id"], et, at, json.dumps(r, ensure_ascii=False), r["source"],
                 now, cid, side, kind, price, qty))
            counts["imported" if cur.rowcount else "skipped"] += 1
    return {"kind": "order_events", "file": str(path), "imported": counts["imported"], "skipped": counts["skipped"],
            "bad_rows": counts["bad_rows"], "first_error": counts.get("first_error")}


def order_kind(con: sqlite3.Connection, account_id: str, broker_order_id: str | None) -> str:
    """一张订单的类型：STOP（含 STOP_LIMIT）、LIMIT、MARKET；没有订单事件时为 UNKNOWN。"""
    if not broker_order_id:
        return "UNKNOWN"
    r = con.execute("SELECT order_kind FROM manual_order_events WHERE account_id=? AND broker_order_id=?"
                    " AND order_kind IS NOT NULL ORDER BY event_time_utc DESC LIMIT 1",
                    (account_id, broker_order_id)).fetchone()
    if r is None:
        return "UNKNOWN"
    return "STOP" if r[0] in ("STOP", "STOP_LIMIT") else r[0]


# ---------------------------------------------------------------- 成交
def _position_before(con, account_id: str, cid: str, at_utc: int, exclude_key: str) -> int:
    r = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN (effect='OPEN') = (side='LONG') THEN quantity ELSE -quantity END), 0)"
        " FROM manual_trades WHERE account_id=? AND contract_id=? AND quarantined=0 AND executed_at_utc<=?"
        " AND fill_key<>?", (account_id, cid, at_utc, exclude_key)).fetchone()
    return r[0]


def _open_lots(con, account_id: str, cid: str, at_utc: int, exclude_key: str) -> list[list]:
    """先进先出的未平仓批次：[[剩余手数, 入场 tick, plan_id, 开仓时刻, trade_id], ...]。"""
    lots: list[list] = []
    for r in con.execute(
            "SELECT trade_id, effect, quantity, price_ticks, plan_id, plan_link_status, executed_at_utc FROM manual_trades"
            " WHERE account_id=? AND contract_id=? AND quarantined=0 AND executed_at_utc<=? AND fill_key<>?"
            " ORDER BY executed_at_utc, imported_at_utc, trade_id", (account_id, cid, at_utc, exclude_key)):
        if r["effect"] == "OPEN":
            lots.append([r["quantity"], r["price_ticks"], r["plan_id"] if r["plan_link_status"] == "LINKED" else None,
                         r["executed_at_utc"], r["trade_id"]])
        else:
            q = r["quantity"]
            while q > 0 and lots:
                take = min(q, lots[0][0])
                lots[0][0] -= take
                q -= take
                if lots[0][0] == 0:
                    lots.pop(0)
    return lots


def _link(con, account_id: str, cid: str, side: str, at_utc: int, plan_id: str | None):
    """开仓成交与事前计划的关联。返回 (状态, plan_id, 待核对建议, 说明)。"""
    if plan_id:
        p = con.execute("SELECT * FROM trader_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if p is None:
            return "UNPLANNED", None, None, f"引用的计划 {plan_id} 不存在"
        if (p["account_id"], p["contract_id"], p["side"]) != (account_id, cid, side):
            return "UNPLANNED", None, None, f"计划 {plan_id} 的账户、合约或方向与成交不符"
        if not plans.was_active_at(con, plan_id, at_utc):
            return "UNPLANNED", None, None, f"事后补录：计划 {plan_id} 在成交时刻尚未激活（或已结束）"
        return "LINKED", plan_id, None, None
    cands = [r["plan_id"] for r in con.execute(
        "SELECT plan_id FROM trader_plans WHERE account_id=? AND contract_id=? AND side=? AND submitted_at_utc<=?",
        (account_id, cid, side, at_utc)) if plans.was_active_at(con, r["plan_id"], at_utc)]
    if len(cands) == 1:
        return "UNKNOWN", None, cands[0], "成交没有带 plan_id；当时有一份相符的已激活计划，待核对"
    if cands:
        return "UNKNOWN", None, None, f"成交没有带 plan_id；当时有 {len(cands)} 份相符的已激活计划，无法确定"
    return "UNPLANNED", None, None, "成交时刻没有相符的已激活计划"


def import_trades(settings: Settings, con: sqlite3.Connection, path: Path, *, risk_cfg: RiskConfig,
                  manual_cfg: ManualConfig) -> dict:
    """导入人工成交。可选列：broker_order_id、broker_fill_id、plan_id。建议先导入订单事件。"""
    rows = _read(Path(path), TRADE_COLUMNS)
    counts: Counter = Counter()
    first_error = None
    parsed = []
    for i, r in enumerate(rows):
        try:
            at = sessions.parse_utc(r["executed_at_utc"])
            cid, inst = _contract(r["contract"], at, settings)
            d = BUY[r["side"].upper()]
            qty = int(r["quantity"])
            if qty <= 0:
                raise ValueError("数量必须为正")
            price = decimal_to_ticks(r["price"], inst.tick_size)
        except (ValueError, KeyError, OffTickPrice, contracts.ContractError) as e:
            counts["bad_rows"] += 1
            first_error = first_error or f"第 {i + 2} 行：{e}"
            continue
        fid = r.get("broker_fill_id") or None
        if fid:
            key, rule = "fill:" + _h(r["source"], fid)[:24], "BROKER_FILL_ID"
        else:
            key, rule = "syn:" + _h(r["source"], r["account_id"], cid, d, qty, at, price,
                                    r.get("broker_order_id"))[:24], "SYNTHETIC_KEY"
        parsed.append({"key": key, "rule": rule, "at": at, "cid": cid, "inst": inst, "d": d, "qty": qty, "price": price,
                       "account": r["account_id"], "order_id": r.get("broker_order_id") or None, "fill_id": fid,
                       "plan_id": r.get("plan_id") or None, "source": r["source"]})
    dup = {k for k, n in Counter(p["key"] for p in parsed if p["rule"] == "SYNTHETIC_KEY").items() if n > 1}
    now = int(time.time())
    touched: set[tuple[str, str]] = set()
    seen_dup: set[str] = set()

    for p in sorted(parsed, key=lambda x: (x["at"], x["key"])):
        if con.execute("SELECT 1 FROM manual_trades WHERE fill_key=? LIMIT 1", (p["key"],)).fetchone():
            counts["skipped"] += 1
            continue
        day = _day(p["at"])
        if p["key"] in dup:                           # 无法唯一去重：隔离，不参与任何统计
            if con.execute("SELECT 1 FROM manual_trades WHERE fill_key=? LIMIT 1", (p["key"] + ":dup",)).fetchone():
                if p["key"] not in seen_dup:          # 这组重复行以前已经隔离过
                    counts["skipped"] += 1
                    continue
            seen_dup.add(p["key"])
            with con:
                n = con.execute("SELECT COUNT(*) FROM manual_trades WHERE fill_key=?", (p["key"],)).fetchone()[0]
                con.execute(
                    "INSERT INTO manual_trades (trade_id, account_id, contract_id, side, quantity, price_ticks,"
                    " executed_at_utc, broker_order_id, broker_fill_id, plan_link_status, source, imported_at_utc,"
                    " fill_key, trading_day, quarantined, match_rule, link_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    ("mt_" + _h(p["key"], "Q", n, time.time_ns())[:24], p["account"], p["cid"],
                     "LONG" if p["d"] > 0 else "SHORT", p["qty"], p["price"], p["at"], p["order_id"], None, "UNKNOWN",
                     p["source"], now, p["key"] + ":dup", day, p["rule"], "没有成交号且文件内重复，无法唯一去重"))
            counts["quarantined"] += 1
            continue
        with con:
            pos = _position_before(con, p["account"], p["cid"], p["at"], p["key"])
            close_qty = min(p["qty"], abs(pos)) if pos * p["d"] < 0 else 0
            open_qty = p["qty"] - close_qty
            if close_qty:
                side = "LONG" if pos > 0 else "SHORT"
                lots = _open_lots(con, p["account"], p["cid"], p["at"], p["key"])
                left, cost, lot_plans, first_open = close_qty, 0, [], None
                for lot in lots:
                    if left == 0:
                        break
                    take = min(left, lot[0])
                    cost += take * lot[1]
                    left -= take
                    lot_plans.append(lot[2])
                    first_open = first_open if first_open is not None else lot[3]
                sd = 1 if side == "LONG" else -1
                gross = sd * (p["price"] * close_qty - cost) * p["inst"].tick_value_usd
                net = gross - risk_cfg.round_trip_fee_usd * close_qty
                kind = order_kind(con, p["account"], p["order_id"])
                tid = "mt_" + _h(p["key"], "CLOSE")[:24]
                plan_ids = {x for x in lot_plans}
                plan_id = next(iter(plan_ids)) if len(plan_ids) == 1 else None
                con.execute(
                    "INSERT INTO manual_trades (trade_id, account_id, contract_id, side, quantity, price_ticks,"
                    " executed_at_utc, broker_order_id, broker_fill_id, plan_id, plan_link_status, source,"
                    " imported_at_utc, fill_key, effect, trading_day, match_rule, entry_ticks, realized_usd, exit_kind)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'CLOSE',?,?,?,?,?)",
                    (tid, p["account"], p["cid"], side, close_qty, p["price"], p["at"], p["order_id"], p["fill_id"],
                     plan_id, "LINKED" if plan_id else "UNKNOWN", p["source"], now, p["key"], day, p["rule"],
                     round(cost / close_qty), str(net), kind))
                risk_mod.record_account_exit(con, risk_cfg, account_id=p["account"], trading_day=day, source_id=tid,
                                             exit_reason="STOP" if kind == "STOP" else "OTHER", net_usd=Decimal(net))
                discipline.on_close(con, settings, manual_cfg, trade_id=tid, opened_at_utc=first_open)
                counts["closes"] += 1
            if open_qty:
                side = "LONG" if p["d"] > 0 else "SHORT"
                status, plan_id, suggestion, note = _link(con, p["account"], p["cid"], side, p["at"], p["plan_id"])
                tid = "mt_" + _h(p["key"], "OPEN")[:24]
                con.execute(
                    "INSERT INTO manual_trades (trade_id, account_id, contract_id, side, quantity, price_ticks,"
                    " executed_at_utc, broker_order_id, broker_fill_id, plan_id, plan_link_status, source,"
                    " imported_at_utc, fill_key, effect, trading_day, match_rule, suggested_plan_id, link_note)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)",
                    (tid, p["account"], p["cid"], side, open_qty, p["price"], p["at"], p["order_id"],
                     None if close_qty else p["fill_id"], plan_id, status, p["source"], now, p["key"], day, p["rule"],
                     suggestion, note))
                if status == "LINKED":
                    plans.record_fill(con, plan_id, open_qty, p["at"])
                discipline.on_open(con, settings, manual_cfg, trade_id=tid)
                counts["opens"] += 1
            counts["imported"] += 1
            touched.add((p["account"], day))
        # 计划满额后释放预留（release 自己开事务）
        for (pid,) in con.execute("SELECT plan_id FROM trader_plans WHERE status='FILLED' AND plan_id IN"
                                  " (SELECT plan_id FROM risk_reservations WHERE status='RESERVED')").fetchall():
            risk_mod.release(con, pid)
    for account, day in sorted(touched):
        discipline.write_session_kpis(con, manual_cfg, account, day)
    return {"kind": "trades", "file": str(path), "imported": counts["imported"], "skipped": counts["skipped"],
            "quarantined": counts["quarantined"], "bad_rows": counts["bad_rows"], "first_error": first_error,
            "open_parts": counts["opens"], "close_parts": counts["closes"],
            "sessions": [f"{a}:{d}" for a, d in sorted(touched)]}
