"""交易日层面的校验与物化：实际合约、所选来源、缺口、RTH 日高低差、参考价位。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from . import contracts, context, sessions
from .config import Settings

THIN_BELOW = 3          # RTH 派生 5 分钟线的构成数低于它时提示 THIN_BAR
DAY_CATEGORIES = ("UNEXPECTED_GAP", "THIN_BAR", "WRONG_CONTRACT")
FAR_FUTURE = 2**62


def pick_source(available: set[str], priority: tuple[str, ...]) -> str | None:
    for s in priority:
        if s in available:
            return s
    return sorted(available)[0] if available else None


def _ranges(slots: list[int], step: int = 300) -> list[tuple[int, int]]:
    out = []
    for s in slots:
        if out and out[-1][1] == s:
            out[-1] = (out[-1][0], s + step)
        else:
            out.append((s, s + step))
    return out


def _anomaly(con, category, source, root, cid, tf, start, end, day, detail):
    con.execute(
        "INSERT OR IGNORE INTO data_anomalies (category, source, instrument_root, contract_id, timeframe,"
        " start_utc, end_utc, trading_day, detail) VALUES (?,?,?,?,?,?,?,?,?)",
        (category, source or "", root, cid or "", tf, start, end, day, detail))


def refresh_day(con: sqlite3.Connection, settings: Settings, root: str, day_s: str):
    day = date.fromisoformat(day_s)
    if not sessions.is_trading_day(day):
        return
    inst = settings.instrument(root)
    cid = contracts.active_contract(inst, day, settings.roll_rules)
    q = ("SELECT DISTINCT source FROM bars WHERE contract_id=? AND timeframe='5m' AND trading_day=?"
         " AND session_type='RTH'")
    source = pick_source({r[0] for r in con.execute(q, (cid, day_s))}, settings.source_priority)

    ph = ",".join("?" * len(DAY_CATEGORIES))
    con.execute(f"DELETE FROM data_anomalies WHERE instrument_root=? AND trading_day=? AND category IN ({ph})",
                (root, day_s, *DAY_CATEGORIES))

    expected = sessions.expected_rth_slots(day)
    present, thin, wrong, fixture = set(), 0, 0, 0
    if source:
        for r in context.visible_bars(con, source, cid, "5m", FAR_FUTURE, trading_day=day_s, session_type="RTH"):
            present.add(r["bar_start_utc"])
            fixture |= r["fixture"]
            if r["constituent_count"] is not None and r["constituent_count"] < THIN_BELOW:
                thin += 1
                _anomaly(con, "THIN_BAR", source, root, cid, "5m", r["bar_start_utc"], r["bar_end_utc"], day_s,
                         f"constituent_count={r['constituent_count']}")
    else:
        others = [r[0] for r in con.execute(
            "SELECT DISTINCT contract_id FROM bars WHERE instrument_root=? AND timeframe='5m' AND trading_day=?"
            " AND session_type='RTH' AND contract_id<>?", (root, day_s, cid))]
        if others:
            wrong = 1
            fixture = con.execute("SELECT MAX(fixture) FROM bars WHERE instrument_root=? AND trading_day=?",
                                  (root, day_s)).fetchone()[0] or 0
            _anomaly(con, "WRONG_CONTRACT", None, root, cid, "5m", expected[0], expected[-1] + 300, day_s,
                     f"{settings.instrument(root).roll_rule_version} 指定实际合约 {cid}，但只有 {', '.join(sorted(others))} 的数据")
    missing = [s for s in expected if s not in present]
    # 最后一根之后的缺失：如果该来源在当日 RTH 收盘之后还有数据，说明是真缺口；
    # 否则这一天还在进行中（或数据到此为止），不登记缺口，只标为未完成。
    in_progress = False
    if source and present and missing and missing[-1] > max(present):
        later = con.execute("SELECT 1 FROM bars WHERE source=? AND instrument_root=? AND bar_start_utc>=? LIMIT 1",
                            (source, root, sessions.rth_bounds(day)[1])).fetchone()
        in_progress = later is None
    counted = [s for s in missing if s < max(present)] if in_progress else missing
    gaps = _ranges(counted) if source else []
    for a, b in gaps:
        _anomaly(con, "UNEXPECTED_GAP", source, root, cid, "5m", a, b, day_s, f"缺 {(b - a) // 300} 根 5 分钟线")

    degraded = 0
    if source:
        row = con.execute("SELECT condition FROM day_quality WHERE source=? AND day=?", (source, day_s)).fetchone()
        degraded = int(bool(row) and row[0] != "available")
    complete = int(bool(source) and not missing)
    reason = None if complete else ("WRONG_CONTRACT" if wrong else "NO_DATA" if not source
                                    else "UNEXPECTED_GAP" if gaps else "DAY_IN_PROGRESS")
    con.execute(
        "INSERT OR REPLACE INTO trading_days (instrument_root, trading_day, contract_id, roll_rule_version, source,"
        " rth_expected, rth_present, gap_count, thin_count, wrong_contract, degraded, early_close, complete, reason,"
        " fixture) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (root, day_s, cid, inst.roll_rule_version, source, len(expected), len(present), len(gaps), thin, wrong,
         degraded, int(sessions.is_early_close(day)), complete, reason, int(fixture)))

    # RTH 日高低差：当日实际合约，以及下一交易日的实际合约（换月后要用它在前一日的数据）
    nxt_cid = contracts.active_contract(inst, sessions.next_trading_day(day), settings.roll_rules)
    for c in {cid, nxt_cid}:
        con.execute("DELETE FROM rth_days WHERE contract_id=? AND trading_day=?", (c, day_s))
        for (s,) in con.execute(q, (c, day_s)).fetchall():
            r = context.rth_day_asof(con, s, c, day, FAR_FUTURE)
            if r:
                con.execute("INSERT INTO rth_days VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (s, c, day_s, r["open"], r["high"], r["low"], r["close"], r["volume"], int(r["complete"]),
                             r["reason"], r["known_at_utc"], json.dumps(r["deps"], separators=(",", ":"))))


def refresh_levels(con: sqlite3.Connection, settings: Settings, root: str, day_s: str):
    day = date.fromisoformat(day_s)
    if not sessions.is_trading_day(day) or context.day_row(con, root, day_s) is None:
        return
    as_of = sessions.rth_bounds(day)[0]
    lv = context.reference_levels_asof(con, settings, root, day, as_of)
    con.execute(
        "INSERT OR REPLACE INTO reference_levels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (root, day_s, lv["contract_id"], lv["source"], lv["pdh"], lv["pdl"], lv["prev_rth_close"], lv["onh"],
         lv["onl"], lv["prev_day_status"], lv["overnight_status"], as_of, lv["known_at_utc"],
         json.dumps(lv["deps"], separators=(",", ":"))))


def refresh(con: sqlite3.Connection, settings: Settings, days: set[tuple[str, str]]):
    """重算受影响交易日的日状态，再重算它们及其后一个交易日的参考价位。"""
    # 前一个交易日也要重算：它末尾的缺失是「进行中」还是「真缺口」取决于之后有没有数据
    days = set(days)
    for root, d in list(days):
        p = sessions.prev_trading_day(date.fromisoformat(d)).isoformat()
        if context.day_row(con, root, p) is not None:
            days.add((root, p))
    todo = sorted(days)
    for i, (root, d) in enumerate(todo):
        refresh_day(con, settings, root, d)
        if i % 500 == 499:
            con.commit()
    con.commit()
    level_days = set(days)
    for root, d in todo:
        try:
            level_days.add((root, sessions.next_trading_day(date.fromisoformat(d)).isoformat()))
        except IndexError:
            pass
    for i, (root, d) in enumerate(sorted(level_days)):
        refresh_levels(con, settings, root, d)
        if i % 500 == 499:
            con.commit()
    con.commit()


def refresh_quality(con: sqlite3.Connection):
    """来源质量状态变化后，重算全部交易日的降级标记。"""
    con.execute("UPDATE trading_days SET degraded = COALESCE((SELECT q.condition <> 'available' FROM day_quality q"
                " WHERE q.source = trading_days.source AND q.day = trading_days.trading_day), 0)")
    con.commit()
