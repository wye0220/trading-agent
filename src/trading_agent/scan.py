"""阶段 1 的最小 scan：只做数据校验和快照封存。不产生信号、纸面订单或成交。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import date

from . import contracts, context, db, sessions
from .config import Scope, Settings
from .days import _ranges, pick_source

DISCLAIMER = "以上为规则化的价格行为学习标注，不构成投资建议。期货有杠杆，风险自负。"
PHASE1_VERSION_TUPLE = {"strategy_family": None, "strategy_version": None, "risk_version": None,
                        "prompt_version": None, "memory_version": None, "engine_version": "phase1-data-only",
                        "model_id": None}


def _session_for(as_of: int) -> tuple[date, bool]:
    """as_of 对应的 RTH 交易日，以及此刻是否在该日 RTH 盘中。盘外时取最近一个已开始的 RTH。"""
    d = sessions.trading_day_at(as_of)
    o, c = sessions.rth_bounds(d)
    if as_of < o:
        d = sessions.prev_trading_day(d)
        o, c = sessions.rth_bounds(d)
    return d, o <= as_of < c


def _snapshot(con, scope: Scope, as_of: int, day: date, cid: str, source: str | None, rows, roll_version: str) -> dict:
    payload = {
        "scope": str(scope), "as_of_utc": as_of, "trading_day": day.isoformat(), "contract_id": cid,
        "source": source, "roll_rule_version": roll_version, "calendar_version": sessions.CALENDAR_VERSION,
        "bars": [[r["bar_start_utc"], r["revision"], r["open"], r["high"], r["low"], r["close"], r["volume"],
                  r["available_at_utc"]] for r in rows],
    }
    sha = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    fixture = int(any(r["fixture"] for r in rows))
    snap = {"snapshot_id": "snap_" + sha[:24], "content_sha256": sha, "bar_count": len(rows), "fixture": bool(fixture),
            "latest_bar_end_utc": rows[-1]["bar_end_utc"] if rows else None,
            "latest_bar_available_at_utc": rows[-1]["available_at_utc"] if rows else None}
    con.execute(
        "INSERT OR IGNORE INTO market_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (snap["snapshot_id"], str(scope), as_of, day.isoformat(), cid, source, len(rows), snap["latest_bar_end_utc"],
         snap["latest_bar_available_at_utc"], roll_version, sessions.CALENDAR_VERSION, sha, fixture, int(time.time())))
    return snap


def _set_block(con, scope: Scope, risk_domain: str, blocked: bool, reason: str | None, as_of: int, run_id: str):
    """更新 scope 的数据阻止状态。状态、冻结事件和告警在同一事务里写入，带 version 校验。"""
    row = con.execute("SELECT blocked, reason, version FROM scope_data_status WHERE scope=? AND risk_domain=?",
                      (str(scope), risk_domain)).fetchone()
    now = int(time.time())
    if row is None:
        con.execute("INSERT INTO scope_data_status VALUES (?,?,?,?,?,1,?)",
                    (str(scope), risk_domain, int(blocked), reason, as_of if blocked else None, now))
        changed = blocked
    else:
        changed = bool(row["blocked"]) != blocked
        cur = con.execute(
            "UPDATE scope_data_status SET blocked=?, reason=?, since_utc=CASE WHEN ? THEN since_utc ELSE ? END,"
            " version=version+1, updated_at_utc=? WHERE scope=? AND risk_domain=? AND version=?",
            (int(blocked), reason, int(not changed), as_of if blocked else None, now, str(scope), risk_domain,
             row["version"]))
        if cur.rowcount != 1:
            raise db.StaleVersion(f"scope_data_status {scope}/{risk_domain} 已被其他进程更新")
    if changed:
        action = "FREEZE" if blocked else "UNFREEZE"
        fid = "frz_" + hashlib.sha256(f"{scope}|{risk_domain}|{action}|{as_of}".encode()).hexdigest()[:20]
        con.execute("INSERT OR IGNORE INTO freeze_events VALUES (?,?,?,?,?,?,?,?)",
                    (fid, str(scope), risk_domain, "DATA", action, reason, "scan", now))
        con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, run_id, created_at_utc)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("CRITICAL" if blocked else "INFO", "DATA_BLOCK" if blocked else "DATA_BLOCK_CLEARED", str(scope),
                     f"{scope} 数据异常，阻止新入场：{reason}" if blocked else f"{scope} 数据恢复，解除数据阻止",
                     json.dumps({"risk_domain": risk_domain, "as_of_utc": as_of}), run_id, now))


def run_scan(settings: Settings, con: sqlite3.Connection, scope: Scope, as_of_utc: int | None = None,
             run_id: str | None = None, *, now_utc: int | None = None, record_event: bool = True) -> dict:
    """数据校验与快照封存。as_of_utc 给出时是历史扫描；否则是盘中（now_utc 仅供测试注入当前时刻）。"""
    explicit = as_of_utc is not None
    as_of = as_of_utc if explicit else (int(time.time()) if now_utc is None else now_utc)
    run_id = run_id or f"scan_{as_of}"
    root = scope.instrument_root
    inst = settings.instrument(root)
    day, in_rth = _session_for(as_of)
    day_s = day.isoformat()
    cid = contracts.active_contract(inst, day, settings.roll_rules)
    o, c = sessions.rth_bounds(day)
    delay = settings.available_at_delay_seconds

    # 按当时可见的数据选来源（每个交易日只用一个）
    avail = {r[0] for r in con.execute(
        "SELECT DISTINCT source FROM bars WHERE contract_id=? AND timeframe=? AND trading_day=? AND session_type='RTH'"
        " AND available_at_utc<=? AND bar_end_utc<=?", (cid, scope.timeframe, day_s, as_of, as_of))}
    source = pick_source(avail, settings.source_priority)
    rows = context.visible_bars(con, source, cid, scope.timeframe, as_of, trading_day=day_s,
                                session_type="RTH") if source else []
    present = {r["bar_start_utc"] for r in rows}
    step = 300 if scope.timeframe == "5m" else 60
    closed = [s for s in sessions.expected_rth_slots(day, step) if s + step + delay <= as_of]
    missing = [s for s in closed if s not in present]

    problems = []
    if not present:
        others = [r[0] for r in con.execute(
            "SELECT DISTINCT contract_id FROM bars WHERE instrument_root=? AND timeframe=? AND trading_day=?"
            " AND session_type='RTH' AND contract_id<>? AND available_at_utc<=?",
            (root, scope.timeframe, day_s, cid, as_of))]
        if others:
            problems.append({"category": "WRONG_CONTRACT",
                             "detail": f"{inst.roll_rule_version} 指定实际合约 {cid}，只有 {', '.join(sorted(others))} 的数据"})
        elif closed:
            problems.append({"category": "NO_DATA", "detail": f"{day_s} RTH 没有 {cid} 的可见数据",
                             "start_utc": closed[0], "end_utc": closed[-1] + step})
    elif missing:
        last = max(present)
        trailing = [s for s in missing if s > last]
        interior = [s for s in missing if s < last]
        for a, b in _ranges(interior, step):
            problems.append({"category": "UNEXPECTED_GAP", "start_utc": a, "end_utc": b,
                             "detail": f"缺 {(b - a) // step} 根 K 线"})
        if trailing:
            n = len(trailing)
            problems.append({"category": "STALE_DATA" if n >= 2 else "LATEST_BAR_MISSING",
                             "start_utc": trailing[0], "end_utc": trailing[-1] + step,
                             "detail": f"新鲜度不足：最新 K 线落后 {n} 根" if n >= 2 else "最近一根已收线的 K 线不存在"})
    for r in con.execute(
            "SELECT start_utc, end_utc, detail FROM data_anomalies WHERE category='CONFLICTING_DUPLICATE'"
            " AND contract_id=? AND timeframe IN ('1m', ?) AND trading_day=? AND start_utc>=? AND start_utc<?",
            (cid, scope.timeframe, day_s, o, min(c, as_of))):
        problems.append({"category": "CONFLICTING_DUPLICATE", "start_utc": r[0], "end_utc": r[1], "detail": r[2]})

    signal_status = "DATA_ERROR" if problems else "WAIT"
    reason = "; ".join(sorted({p["category"] for p in problems})) or None
    q = con.execute("SELECT condition FROM day_quality WHERE source=? AND day=?", (source or "", day_s)).fetchone()
    degraded = bool(q) and q[0] != "available"

    with con:
        snap = _snapshot(con, scope, as_of, day, cid, source, rows, inst.roll_rule_version)
        run_mode = "fixture" if snap["fixture"] else ("historical_replay" if explicit else "paper")
        _set_block(con, scope, run_mode, bool(problems), reason, as_of, run_id)
        levels = context.reference_levels_asof(con, settings, root, day, as_of)
        adr_days = context.rth_day_ranges_asof(con, settings, root, as_of, 10)
        hourly = context.hourly_asof(con, source, cid, as_of, 10) if source else []
        decision = {
            "phase": 1, "note": "阶段 1 只做数据校验，不产生交易信号",
            "problems": problems, "degraded_day": degraded, "in_rth": in_rth,
            "next_check": "数据恢复后重新扫描" if problems else "下一根 K 线收线后（策略规则由阶段 2 提供）",
            "reference_levels": {k: levels[k] for k in ("pdh", "pdl", "prev_rth_close", "onh", "onl",
                                                         "prev_day_status", "overnight_status", "known_at_utc")},
            "adr_valid_days": sum(1 for d in adr_days if d.get("complete")),
            "hourly_bars_available": len(hourly),
        }
        event_id = "scan_" + hashlib.sha256(
            f"{scope}|{as_of}|{snap['snapshot_id']}|{run_mode}".encode()).hexdigest()[:24]
        # record_event=False：调用方（scanner）会经引擎写它自己的扫描记录，这里只做校验和快照
        (con.execute if record_event else (lambda *a, **k: None))(
            "INSERT OR IGNORE INTO scan_events (event_id, scope, strategy_family, run_mode, risk_domain, run_id,"
            " as_of_utc, snapshot_id, contract_id, signal_status, risk_status, paper_action, version_tuple_json,"
            " strategy_state_json, decision_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, str(scope), scope.strategy_family, run_mode, run_mode, run_id, as_of, snap["snapshot_id"], cid,
             signal_status, "NOT_EVALUATED", "NONE",
             json.dumps({**PHASE1_VERSION_TUPLE, "strategy_family": scope.strategy_family}), "{}",
             json.dumps(decision, ensure_ascii=False), int(time.time())))

    tick = inst.tick_size
    px = lambda t: None if t is None else str(t * tick)   # noqa: E731
    last = rows[-1] if rows else None
    result = {
        "scope": str(scope), "as_of_utc": sessions.iso(as_of), "run_mode": run_mode, "event_id": event_id,
        "signal_status": signal_status, "risk_status": "NOT_EVALUATED", "paper_action": "NONE",
        "new_entries_blocked": bool(problems), "block_reason": reason,
        "trading_day": day_s, "contract_id": cid, "source": source, "in_rth": in_rth,
        "roll_rule_version": inst.roll_rule_version, "calendar_version": sessions.CALENDAR_VERSION,
        "snapshot_id": snap["snapshot_id"], "snapshot_sha256": snap["content_sha256"], "fixture": snap["fixture"],
        "bar_count": len(rows),
        "latest_bar": None if last is None else {
            "bar_start_utc": sessions.iso(last["bar_start_utc"]), "bar_end_utc": sessions.iso(last["bar_end_utc"]),
            "available_at_utc": sessions.iso(last["available_at_utc"]),
            "available_at_estimated": bool(last["available_at_estimated"]), "revision": last["revision"],
            "open": px(last["open"]), "high": px(last["high"]), "low": px(last["low"]), "close": px(last["close"]),
            "volume": last["volume"]},
        "problems": [{**p, **{k: sessions.iso(p[k]) for k in ("start_utc", "end_utc") if k in p}} for p in problems],
        "degraded_day": degraded,
        "reference_levels": {**{k: px(levels[k]) for k in ("pdh", "pdl", "prev_rth_close", "onh", "onl")},
                             "prev_day_status": levels["prev_day_status"],
                             "overnight_status": levels["overnight_status"]},
        "adr_valid_days": decision["adr_valid_days"], "hourly_bars_available": len(hourly),
        "version_tuple": {**PHASE1_VERSION_TUPLE, "strategy_family": scope.strategy_family},
    }
    result["report"] = short_report(result)
    return result


def _et(iso_text: str | None) -> str:
    if not iso_text:
        return "—"
    return sessions.et(sessions.parse_utc(iso_text)).strftime("%H:%M:%S")


def short_report(r: dict) -> str:
    as_of_et = sessions.et(sessions.parse_utc(r["as_of_utc"])).strftime("%Y-%m-%d %H:%M:%S ET")
    tag = " [FIXTURE]" if r["fixture"] else ""
    lb = r["latest_bar"]
    lv = r["reference_levels"]
    lines = [f"[{as_of_et}] [{r['scope']}] [{r['contract_id']}] [{r['signal_status']}] [engine=phase1-data-only]{tag}"]
    if lb:
        lines.append(f"事实：最新 K 线 {_et(lb['bar_start_utc'])}–{_et(lb['bar_end_utc'])} ET，"
                     f"开 {lb['open']} 高 {lb['high']} 低 {lb['low']} 收 {lb['close']}，量 {lb['volume']}。"
                     f"PDH {lv['pdh'] or '不可用'} / PDL {lv['pdl'] or '不可用'} / 上一 RTH 收盘 {lv['prev_rth_close'] or '不可用'}，"
                     f"ONH {lv['onh'] or '不可用'} / ONL {lv['onl'] or '不可用'}。"
                     f"ADR 有效日 {r['adr_valid_days']}/10，已完结 1 小时线 {r['hourly_bars_available']} 根。")
    else:
        lines.append("事实：没有可见的 K 线。")
    if r["signal_status"] == "DATA_ERROR":
        det = "；".join(f"{p['category']}（{p.get('detail', '')}）" for p in r["problems"])
        lines.append(f"决策：DATA_ERROR，该 scope 不可新入场。原因：{det}。下一确认点：数据恢复后重新扫描。")
    else:
        lines.append("决策：WAIT。阶段 1 只做数据校验，没有策略规则，不产生信号；缺失条件是策略插件（阶段 2）。")
    lines.append("持仓：阶段 1 没有纸面账本。")
    lines.append("纪律：阶段 1 不评估。")
    src = r["source"] or "无"
    lines.append(f"数据：快照 {r['snapshot_id']}，来源 {src}，{r['bar_count']} 根；"
                 + (f"最新 bar 完成 {_et(lb['bar_end_utc'])} ET，到达 {_et(lb['available_at_utc'])} ET"
                    f"{'（估计）' if lb['available_at_estimated'] else ''}。" if lb else "没有最新 bar。")
                 + ("当日被来源标为降级日。" if r["degraded_day"] else ""))
    lines.append(DISCLAIMER)
    return "\n".join(lines)
