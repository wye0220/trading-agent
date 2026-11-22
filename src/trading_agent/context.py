"""按决策时点查询行情背景。只返回当时已可见的数据和当时的 revision。"""
from __future__ import annotations

import sqlite3
from datetime import date

from . import contracts, sessions
from .config import Settings

_COLS = ("bar_start_utc, bar_end_utc, revision, open, high, low, close, volume, available_at_utc,"
         " available_at_estimated, session_type, trading_day, constituent_count, fixture, deps_json")


def visible_bars(con: sqlite3.Connection, source: str, contract_id: str, timeframe: str, as_of_utc: int, *,
                 trading_day: str | None = None, session_type: str | None = None,
                 start_utc: int | None = None, end_utc: int | None = None,
                 limit: int | None = None, newest_first: bool = False) -> list[sqlite3.Row]:
    """as_of 时已收线且已可见的 K 线，每根取当时可见的最高 revision。"""
    sql = [f"SELECT {_COLS} FROM bars b WHERE source=? AND contract_id=? AND timeframe=?"
           " AND bar_end_utc<=? AND available_at_utc<=?"
           " AND revision=(SELECT MAX(revision) FROM bars x WHERE x.source=b.source AND x.contract_id=b.contract_id"
           "   AND x.timeframe=b.timeframe AND x.bar_start_utc=b.bar_start_utc AND x.available_at_utc<=?)"]
    args: list = [source, contract_id, timeframe, as_of_utc, as_of_utc, as_of_utc]
    if trading_day is not None:
        # 用起始时刻范围收窄主键扫描：上一交易日 Globex 收盘之后、当日 Globex 收盘之前
        d = date.fromisoformat(trading_day)
        lo = sessions.eth_bounds(sessions.prev_trading_day(d))[1]
        hi = sessions.eth_bounds(d)[1]
        sql.append("AND bar_start_utc>=? AND bar_start_utc<?")
        args += [lo, hi]
    for cond, val in (("trading_day=?", trading_day), ("session_type=?", session_type),
                      ("bar_start_utc>=?", start_utc), ("bar_start_utc<?", end_utc)):
        if val is not None:
            sql.append("AND " + cond)
            args.append(val)
    sql.append("ORDER BY bar_start_utc" + (" DESC" if newest_first else ""))
    if limit:
        sql.append("LIMIT ?")
        args.append(limit)
    return con.execute(" ".join(sql), args).fetchall()


def day_row(con: sqlite3.Connection, root: str, day: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM trading_days WHERE instrument_root=? AND trading_day=?", (root, day)).fetchone()


def rth_day_asof(con: sqlite3.Connection, source: str, contract_id: str, day: date, as_of_utc: int) -> dict | None:
    """某 RTH 交易日的开高低收，只用 as_of 时可见的 5 分钟线。该日 RTH 尚未结束或没有数据时返回 None。"""
    if not sessions.is_trading_day(day):
        return None
    o, c = sessions.rth_bounds(day)
    if as_of_utc < c:
        return None
    rows = visible_bars(con, source, contract_id, "5m", as_of_utc, trading_day=day.isoformat(), session_type="RTH")
    if not rows:
        return None
    present = {r["bar_start_utc"] for r in rows}
    missing = [s for s in sessions.expected_rth_slots(day) if s not in present]
    return {
        "trading_day": day.isoformat(), "source": source, "contract_id": contract_id,
        "open": rows[0]["open"], "high": max(r["high"] for r in rows), "low": min(r["low"] for r in rows),
        "close": rows[-1]["close"], "volume": sum(r["volume"] for r in rows),
        "range_ticks": max(r["high"] for r in rows) - min(r["low"] for r in rows),
        "complete": not missing, "reason": None if not missing else f"UNEXPECTED_GAP:{len(missing)}",
        "known_at_utc": max(r["available_at_utc"] for r in rows),
        "deps": [[r["bar_start_utc"], r["revision"]] for r in rows],
        "fixture": any(r["fixture"] for r in rows),
    }


def rth_day_ranges_asof(con: sqlite3.Connection, settings: Settings, root: str, as_of_utc: int, n: int = 10) -> list[dict]:
    """当前交易日之前的 n 个 RTH 交易日（供 ADR）。当前未结束的交易日不在其中。

    每项带 complete 标记；不完整或没有数据的交易日也列出并给出原因，由使用方决定有效日数。
    """
    inst = settings.instrument(root)
    cur = sessions.trading_day_at(as_of_utc)
    out = []
    d = cur
    for _ in range(n):
        d = sessions.prev_trading_day(d)
        row = day_row(con, root, d.isoformat())
        if row is None or row["source"] is None:
            out.append({"trading_day": d.isoformat(), "complete": False, "reason": "NO_DATA"})
            continue
        r = rth_day_asof(con, row["source"], row["contract_id"], d, as_of_utc)
        out.append(r or {"trading_day": d.isoformat(), "complete": False, "reason": "NOT_VISIBLE"})
    return out


def hourly_asof(con: sqlite3.Connection, source: str, contract_id: str, as_of_utc: int, n: int = 10) -> list[sqlite3.Row]:
    """as_of 时已完结且可见的最近 n 根 1 小时线（时间升序）。"""
    rows = visible_bars(con, source, contract_id, "1h", as_of_utc, limit=n, newest_first=True)
    return rows[::-1]


def reference_levels_asof(con: sqlite3.Connection, settings: Settings, root: str, day: date, as_of_utc: int) -> dict:
    """PDH、PDL、上一 RTH 收盘、ONH、ONL。全部取自当日实际合约；不满足时标为不可用并给原因。"""
    inst = settings.instrument(root)
    cid = contracts.active_contract(inst, day, settings.roll_rules)
    row = day_row(con, root, day.isoformat())
    source = row["source"] if row else None
    out = {"instrument_root": root, "trading_day": day.isoformat(), "contract_id": cid, "source": source,
           "pdh": None, "pdl": None, "prev_rth_close": None, "onh": None, "onl": None,
           "prev_day_status": "UNAVAILABLE:NO_DATA", "overnight_status": "UNAVAILABLE:NO_DATA",
           "as_of_utc": as_of_utc, "known_at_utc": None, "deps": {"prev_day": None, "overnight": []}}
    known = []

    prev = sessions.prev_trading_day(day)
    # 前一日的数据必须来自同一实际合约。来源按前一日所选来源优先，其次当日来源。
    prow = day_row(con, root, prev.isoformat())
    cands = [s for s in ((prow["source"] if prow else None), source) if s]
    cands += [s for s in settings.source_priority if s not in cands]
    pd = None
    for s in cands:
        pd = rth_day_asof(con, s, cid, prev, as_of_utc)
        if pd:
            break
    if pd is None:
        rolled = contracts.active_contract(inst, prev, settings.roll_rules) != cid
        out["prev_day_status"] = "UNAVAILABLE:" + ("CONTRACT_ROLLED_NO_PRIOR_DATA" if rolled else "NO_DATA")
    elif not pd["complete"]:
        out["prev_day_status"] = "UNAVAILABLE:PREV_DAY_INCOMPLETE"
    else:
        out.update(pdh=pd["high"], pdl=pd["low"], prev_rth_close=pd["close"], prev_day_status="OK")
        out["deps"]["prev_day"] = {"source": pd["source"], "contract_id": cid, "trading_day": prev.isoformat(),
                                   "bars": pd["deps"]}
        known.append(pd["known_at_utc"])

    if source and sessions.is_trading_day(day):
        rth_open = sessions.rth_bounds(day)[0]
        on = visible_bars(con, source, cid, "5m", as_of_utc, trading_day=day.isoformat(), session_type="ETH",
                          end_utc=rth_open)
        if on:
            out.update(onh=max(r["high"] for r in on), onl=min(r["low"] for r in on), overnight_status="OK")
            out["deps"]["overnight"] = [[r["bar_start_utc"], r["revision"]] for r in on]
            known.append(max(r["available_at_utc"] for r in on))
        else:
            out["overnight_status"] = "UNAVAILABLE:NO_OVERNIGHT_DATA"
    out["known_at_utc"] = max(known) if known else None
    return out
