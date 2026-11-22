"""派生 K 线：1 分钟 → 5 分钟，5 分钟 → 已完结 1 小时。

派生线的 available_at_utc 就是它的 known_at_utc：全部依赖中最晚的可用时点。依赖出现新 revision 时，
派生线也产生新 revision，旧的保留，所以按决策时点查询能拿到当时的版本。
对相同的原始数据和配置，结果完全相同（先删后建）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from . import contracts, sessions
from .config import Settings

HOUR_INCOMPLETE = "INCOMPLETE_HOUR"


def _aggregate(buckets: dict[int, dict[int, list]], size: int, delay: int):
    """buckets: 桶起点 -> {下级起点 -> [按 revision 升序的行]}。产出派生线的各个 revision。

    行：(start, revision, o, h, l, c, v, avail, est, fixture, session)
    """
    for bstart in sorted(buckets):
        cons = buckets[bstart]
        any_est = any(revs[0][8] for revs in cons.values())
        t0 = max(bstart + size + (delay if any_est else 0), max(revs[0][7] for revs in cons.values()))
        later = sorted({r[7] for revs in cons.values() for r in revs[1:] if r[7] > t0})
        prev = None
        rev_no = 0
        for t in [t0, *later]:
            vis = []
            for start in sorted(cons):
                pick = None
                for r in cons[start]:
                    if r[7] <= t:
                        pick = r
                if pick is not None:
                    vis.append(pick)
            if not vis:
                continue
            content = (vis[0][2], max(r[3] for r in vis), min(r[4] for r in vis), vis[-1][5], sum(r[6] for r in vis))
            deps = [[r[0], r[1]] for r in vis]
            if (content, deps) == prev:
                continue
            prev = (content, deps)
            rev_no += 1
            sess = {r[10] for r in vis}
            yield (bstart, rev_no, bstart + size, sess.pop() if len(sess) == 1 else "MIX", *content, t,
                   int(any(r[8] for r in vis)), len(vis), json.dumps(deps, separators=(",", ":")),
                   int(any(r[9] for r in vis)))


def _load(con, source, cid, tf, day, size):
    buckets: dict[int, dict[int, list]] = {}
    for r in con.execute(
            "SELECT bar_start_utc, revision, open, high, low, close, volume, available_at_utc,"
            " available_at_estimated, fixture, session_type FROM bars"
            " WHERE source=? AND contract_id=? AND timeframe=? AND trading_day=? ORDER BY bar_start_utc, revision",
            (source, cid, tf, day)):
        buckets.setdefault(r[0] - r[0] % size, {}).setdefault(r[0], []).append(tuple(r))
    return buckets


def _store(con, source, cid, root, tf, day, rows):
    con.execute("DELETE FROM bars WHERE source=? AND contract_id=? AND timeframe=? AND trading_day=? AND derived=1",
                (source, cid, tf, day))
    con.executemany(
        "INSERT INTO bars (source, contract_id, timeframe, bar_start_utc, revision, instrument_root, bar_end_utc,"
        " trading_day, session_type, open, high, low, close, volume, available_at_utc, available_at_estimated,"
        " derived, constituent_count, deps_json, fixture) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
        [(source, cid, tf, r[0], r[1], root, r[2], day, r[3], *r[4:9], r[9], r[10], r[11], r[12], r[13]) for r in rows])


def rebuild_day(con: sqlite3.Connection, settings: Settings, source: str, cid: str, root: str, day: str):
    delay = settings.available_at_delay_seconds
    # 5 分钟线：只有来源的原始层是 1 分钟线时才聚合；来源原生就是 5 分钟线则原样使用。
    b1 = _load(con, source, cid, "1m", day, 300)
    if b1:
        _store(con, source, cid, root, "5m", day, list(_aggregate(b1, 300, delay)))
    # 1 小时线：整点对齐。小时内 RTH 预期的 5 分钟线不全则不生成。
    b5 = _load(con, source, cid, "5m", day, 3600)
    con.execute("DELETE FROM data_anomalies WHERE category=? AND source=? AND contract_id=? AND trading_day=?",
                (HOUR_INCOMPLETE, source, cid, day))
    d = date.fromisoformat(day)
    expected = set(sessions.expected_rth_slots(d)) if sessions.is_trading_day(d) else set()
    # 只为当日实际合约登记「小时不完整」，远月合约本来就稀疏
    is_active = bool(expected) and contracts.active_contract(
        settings.instrument(root), d, settings.roll_rules) == cid
    ok = {}
    for bstart, cons in b5.items():
        missing = sorted(s for s in expected if bstart <= s < bstart + 3600 and s not in cons)
        if missing and is_active:
            con.execute(
                "INSERT OR IGNORE INTO data_anomalies (category, source, instrument_root, contract_id, timeframe,"
                " start_utc, end_utc, trading_day, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                (HOUR_INCOMPLETE, source, root, cid, "1h", bstart, bstart + 3600, day,
                 f"小时内缺 {len(missing)} 根 RTH 5 分钟线，不生成 1 小时线"))
        if not missing:
            ok[bstart] = cons
    _store(con, source, cid, root, "1h", day, list(_aggregate(ok, 3600, delay)))


def rebuild(con: sqlite3.Connection, settings: Settings, touched: set[tuple[str, str, str]]) -> set[tuple[str, str]]:
    """重建受影响的 (source, contract_id, trading_day) 的派生线。返回受影响的 (instrument_root, trading_day)。"""
    days = set()
    for i, (source, cid, day) in enumerate(sorted(touched)):
        row = con.execute("SELECT instrument_root FROM bars WHERE source=? AND contract_id=? AND trading_day=? LIMIT 1",
                          (source, cid, day)).fetchone()
        if row is None:
            continue
        rebuild_day(con, settings, source, cid, row[0], day)
        days.add((row[0], day))
        if i % 500 == 499:
            con.commit()
    con.commit()
    return days


def clear_derived(con: sqlite3.Connection):
    """清空全部派生线（用于验证可重现）。"""
    con.execute("DELETE FROM bars WHERE derived = 1")
    con.execute("DELETE FROM data_anomalies WHERE category = ?", (HOUR_INCOMPLETE,))
    con.commit()


def all_raw_keys(con: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {tuple(r) for r in con.execute("SELECT DISTINCT source, contract_id, trading_day FROM bars WHERE derived = 0")}
