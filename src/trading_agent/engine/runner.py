"""逐步运行器：把"推进行情 → 算特征 → 调策略 → 风控 → 挂单"串成一步。scan 和 replay 都调用它。

一步在一个事务里提交。同一个运行、同一根信号 K 重跑是空操作。时钟只前进。
引擎只通过 StrategyProvider 接口与策略交互。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import date
from decimal import Decimal

from .. import contracts, context, features, sessions
from ..config import Scope, Settings, parse_scope
from ..days import pick_source
from ..scan import _session_for, _snapshot
from ..strategies.base import ClosedTrade, StrategyState, TargetSpec
from . import fills, risk as risk_mod, versions
from .config import EngineConfig, RiskConfig, load_engine, load_risk
from .fills import Book, FillConfig, Position, SubBar, WorkingOrder

TIMEFRAME_SECONDS = {"5m": 300}
WARMUP_BARS = 400


class ClockError(RuntimeError):
    """决策时点早于运行的水位：时钟只前进。"""


class ReplayRefused(RuntimeError):
    """拒绝在未划定开发边界的真实数据上回放。"""


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


# ---------------------------------------------------------------- 运行与冻结
def create_run(con: sqlite3.Connection, scope: Scope, strategy, run_mode: str, run_id: str | None = None) -> str:
    run_id = run_id or "run_" + uuid.uuid4().hex[:16]
    now = int(time.time())
    with con:
        con.execute("INSERT INTO engine_runs (run_id, scope, strategy_family, run_mode, risk_domain, state_json,"
                    " created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, str(scope), strategy.strategy_family, run_mode, f"{run_mode}:{run_id}",
                     strategy.initial_state().to_json(), now, now))
    return run_id


def get_run(con: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    r = con.execute("SELECT * FROM engine_runs WHERE run_id=?", (run_id,)).fetchone()
    if r is None:
        raise KeyError(f"没有这个运行：{run_id}")
    return r


def set_freeze(con: sqlite3.Connection, scope: Scope | str, risk_domain: str, *, frozen: bool, cause: str = "MANUAL",
               reason: str | None = None, actor: str | None = None) -> str:
    """登记一次冻结或解除。risk_domain 可以是某个运行的风险域，或 '*'（该 scope 的所有风险域）。"""
    now = int(time.time())
    fid = "frz_" + _h(scope, risk_domain, cause, frozen, now, uuid.uuid4().hex)[:20]
    with con:
        con.execute("INSERT INTO freeze_events VALUES (?,?,?,?,?,?,?,?)",
                    (fid, str(scope), risk_domain, cause, "FREEZE" if frozen else "UNFREEZE", reason, actor, now))
    return fid


def frozen_reason(con: sqlite3.Connection, scope: Scope | str, risk_domain: str) -> str | None:
    """该 scope 在这个风险域里是否不可新入场。按原因分别看最近一次事件；另看数据阻止状态。"""
    rows = con.execute(
        "SELECT cause, action FROM freeze_events WHERE scope=? AND risk_domain IN (?, '*')"
        " ORDER BY created_at_utc, rowid", (str(scope), risk_domain)).fetchall()
    last: dict[str, str] = {}
    for r in rows:
        last[r["cause"]] = r["action"]
    active = sorted(c for c, a in last.items() if a == "FREEZE")
    if active:
        return "FROZEN:" + ",".join(active)
    r = con.execute("SELECT blocked, reason FROM scope_data_status WHERE scope=? AND risk_domain=?",
                    (str(scope), risk_domain)).fetchone()
    if r is not None and r["blocked"]:
        return "DATA_BLOCKED:" + (r["reason"] or "")
    return None


# ---------------------------------------------------------------- 订单簿的读写
def _load_book(con: sqlite3.Connection, run_id: str) -> Book:
    orders = []
    for r in con.execute("SELECT * FROM paper_orders WHERE run_id=? AND status IN ('ARMED','TRIGGERED')"
                         " ORDER BY armed_at_utc, order_id", (run_id,)):
        t = json.loads(r["target_json"])
        value = t["value"]
        if not isinstance(value, int):
            from fractions import Fraction
            value = Fraction(value)
        extra = json.loads(r["intent_json"])
        orders.append(WorkingOrder(
            order_id=r["order_id"], setup_id=r["setup_id"], side=r["side"], order_type=r["order_type"],
            stop_ticks=r["stop_ticks"], target=TargetSpec(t["kind"], value), armed_at_utc=r["armed_at_utc"],
            expires_at_utc=r["expires_at_utc"], flatten_at_utc=extra["flatten_at_utc"],
            trigger_ticks=r["trigger_ticks"], limit_ticks=r["limit_ticks"], status=r["status"],
            bars_seen=r["bars_seen"], flags=tuple(json.loads(r["flags_json"]))))
    p = con.execute("SELECT * FROM paper_positions WHERE run_id=? AND status='OPEN'", (run_id,)).fetchone()
    pos = None if p is None else Position(
        position_id=p["position_id"], order_id=p["order_id"], setup_id=p["setup_id"], side=p["side"],
        entry_ticks=p["entry_ticks"], entry_time_utc=p["entry_time_utc"], initial_stop_ticks=p["initial_stop_ticks"],
        stop_ticks=p["stop_ticks"], target_ticks=p["target_ticks"], flatten_at_utc=p["flatten_at_utc"],
        flags=tuple(json.loads(p["flags_json"])))
    return Book(tuple(orders), pos)


def _persist(con, run, scope: Scope, inst, cid: str, day_s: str, risk_cfg: RiskConfig, before: Book, after: Book,
             events: list) -> list[dict]:
    """把成交模型的结果写进账本。返回事件的可序列化摘要。"""
    now = int(time.time())
    run_id, domain = run["run_id"], run["risk_domain"]
    old = {o.order_id: o for o in before.orders}
    for o in after.orders:
        if old.get(o.order_id) != o:
            con.execute("UPDATE paper_orders SET status=?, bars_seen=?, flags_json=?, end_reason=?,"
                        " version=version+1, updated_at_utc=? WHERE order_id=?",
                        (o.status, o.bars_seen, json.dumps(list(o.flags)), o.end_reason, now, o.order_id))
    out = []
    for e in events:
        out.append({"kind": e.kind, "time_utc": e.time_utc, "order_id": e.order_id, "position_id": e.position_id,
                    "price_ticks": e.price_ticks, "reason": e.reason, "flags": list(e.flags)})
        if e.kind == "ENTRY":
            p = e.position
            o = con.execute("SELECT version_tuple_json, quantity, setup_id FROM paper_orders WHERE order_id=?",
                            (p.order_id,)).fetchone()
            con.execute(
                "INSERT INTO paper_positions (position_id, run_id, scope, risk_domain, order_id, setup_id, contract_id,"
                " trading_day, side, quantity, entry_ticks, entry_time_utc, initial_stop_ticks, stop_ticks,"
                " target_ticks, flatten_at_utc, status, flags_json, version_tuple_json, created_at_utc, updated_at_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)",
                (p.position_id, run_id, str(scope), domain, p.order_id, p.setup_id, cid, day_s, p.side, o["quantity"],
                 p.entry_ticks, p.entry_time_utc, p.initial_stop_ticks, p.stop_ticks, p.target_ticks, p.flatten_at_utc,
                 json.dumps(list(p.flags)), o["version_tuple_json"], now, now))
            con.execute(
                "INSERT INTO paper_fills (fill_id, order_id, scope, risk_domain, run_id, contract_id, side, quantity,"
                " price_ticks, fill_time_utc, flags_json, fees_json, created_at_utc, position_id, kind, reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ENTRY',?)",
                ("fill_" + _h(p.order_id, "ENTRY")[:20], p.order_id, str(scope), domain, run_id, cid, p.side,
                 o["quantity"], p.entry_ticks, p.entry_time_utc, json.dumps(list(e.flags)), "{}", now, p.position_id,
                 e.reason))
        elif e.kind == "EXIT":
            p = e.position
            row = con.execute("SELECT * FROM paper_positions WHERE position_id=?", (p.position_id,)).fetchone()
            qty = row["quantity"]
            res = risk_mod.settle_trade(inst, risk_cfg, side=p.side, quantity=qty, entry_ticks=p.entry_ticks,
                                        exit_ticks=e.price_ticks, initial_stop_ticks=p.initial_stop_ticks, flags=e.flags)
            con.execute("UPDATE paper_positions SET status='CLOSED', flags_json=?, version=version+1, updated_at_utc=?"
                        " WHERE position_id=? AND status='OPEN'", (json.dumps(list(res["flags"])), now, p.position_id))
            exit_side = "SHORT" if p.side == "LONG" else "LONG"
            con.execute(
                "INSERT INTO paper_fills (fill_id, order_id, scope, risk_domain, run_id, contract_id, side, quantity,"
                " price_ticks, fill_time_utc, flags_json, fees_json, created_at_utc, position_id, kind, reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'EXIT',?)",
                ("fill_" + _h(p.order_id, "EXIT")[:20], p.order_id, str(scope), domain, run_id, cid, exit_side, qty,
                 e.price_ticks, e.time_utc, json.dumps(list(res["flags"])),
                 json.dumps({"round_trip_usd": str(res["fees_usd"])}), now, p.position_id, e.reason))
            s = lambda v: None if v is None else str(v)      # noqa: E731
            con.execute(
                "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.position_id, run_id, str(scope), domain, p.setup_id, row["contract_id"], row["trading_day"], p.side,
                 qty, p.entry_ticks, p.entry_time_utc, e.price_ticks, e.time_utc, e.reason, res["initial_risk_ticks"],
                 res["pnl_ticks"], str(res["gross_usd"]), str(res["fees_usd"]), str(res["net_usd"]), res["net_sign"],
                 s(res["r_initial"]), s(res["r_research"]), json.dumps(list(res["flags"])), row["version_tuple_json"],
                 now))
        elif e.kind == "NOTE":
            con.execute("UPDATE paper_positions SET flags_json=?, version=version+1, updated_at_utc=?"
                        " WHERE position_id=?", (json.dumps(list(e.flags)), now, e.position_id))
            con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, run_id, created_at_utc)"
                        " VALUES ('CRITICAL',?,?,?,?,?,?)",
                        (e.reason, str(scope), f"{scope} 截止时刻之后没有行情，纸面仓位无法平仓，标为无法定价",
                         json.dumps({"position_id": e.position_id}), run_id, now))
    pos = after.position
    if pos is not None:
        con.execute("UPDATE paper_positions SET flags_json=?, stop_ticks=?, updated_at_utc=? WHERE position_id=?"
                    " AND flags_json<>?", (json.dumps(list(pos.flags)), pos.stop_ticks, now, pos.position_id,
                                           json.dumps(list(pos.flags))))
    return out


# ---------------------------------------------------------------- 一步
def step(settings: Settings, con: sqlite3.Connection, run_id: str, strategy, as_of_utc: int, *,
         risk_cfg: RiskConfig | None = None, engine_cfg: EngineConfig | None = None,
         release_con: sqlite3.Connection | None = None) -> dict:
    """以 as_of_utc 为决策时点推进一步。"""
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    if con.in_transaction:
        raise RuntimeError("step 需要自己开事务；调用前请先提交或回滚")
    con.execute("BEGIN IMMEDIATE")
    try:
        result = _step(settings, con, run_id, strategy, as_of_utc, risk_cfg, engine_cfg, release_con)
        con.commit()
        return result
    except BaseException:
        con.rollback()
        raise


def _step(settings, con, run_id, strategy, as_of, risk_cfg, engine_cfg, release_con) -> dict:
    run = get_run(con, run_id)
    if as_of < run["watermark_utc"]:
        raise ClockError(f"决策时点 {sessions.iso(as_of)} 早于运行的水位 {sessions.iso(run['watermark_utc'])}")
    if strategy.strategy_family != run["strategy_family"]:
        raise ValueError(f"运行 {run_id} 属于策略 {run['strategy_family']}，不能用 {strategy.strategy_family} 步进")
    scope = parse_scope(run["scope"], settings)
    tf = TIMEFRAME_SECONDS[scope.timeframe]
    inst = settings.instrument(scope.instrument_root)
    vt = versions.current(scope, strategy, risk_cfg, release_con)         # 每一步加载一次
    day, _ = _session_for(as_of)
    day_s = day.isoformat()
    cid = contracts.active_contract(inst, day, settings.roll_rules)
    rth_open, rth_close = sessions.rth_bounds(day)
    cutoff = rth_close - engine_cfg.flatten_minutes_before_close * 60

    avail = {r[0] for r in con.execute(
        "SELECT DISTINCT source FROM bars WHERE contract_id=? AND timeframe=? AND trading_day=? AND session_type='RTH'"
        " AND available_at_utc<=? AND bar_end_utc<=?", (cid, scope.timeframe, day_s, as_of, as_of))}
    source = pick_source(avail, settings.source_priority)
    day_rows = context.visible_bars(con, source, cid, scope.timeframe, as_of, trading_day=day_s,
                                    session_type="RTH") if source else []
    signal = day_rows[-1] if day_rows else None

    base = {"run_id": run_id, "scope": str(scope), "as_of_utc": as_of, "trading_day": day_s, "contract_id": cid,
            "source": source, "version_tuple": vt}
    if signal is not None and signal["bar_end_utc"] <= run["last_bar_end_utc"]:
        stored = con.execute("SELECT decision_json FROM scan_events WHERE event_id=?",
                             ("scan_" + _h(run_id, signal["bar_end_utc"])[:24],)).fetchone()
        if stored is not None:                        # 同一根信号 K 已处理过：空操作
            return {**json.loads(stored[0]), "status": "ALREADY_PROCESSED"}
        return {**base, "status": "ALREADY_PROCESSED", "events": []}

    # 待处理的信号 K：上次处理之后、此刻已可见的每一根，按时间顺序逐根处理。
    # 落下的 K（睡眠、停机之后补算）照样喂给策略以保持状态连续，但它们的信号多半已经过期，不会变成订单。
    pending = [r for r in day_rows if r["bar_end_utc"] > run["last_bar_end_utc"]]
    has_1m = bool(source) and con.execute(
        "SELECT 1 FROM bars WHERE source=? AND contract_id=? AND timeframe='1m' AND trading_day=? AND derived=0 LIMIT 1",
        (source, cid, day_s)).fetchone() is not None
    fill_cfg = FillConfig(risk_cfg.entry_slippage_ticks, risk_cfg.stop_slippage_ticks, market_fill_on_straddle=not has_1m)
    subs = context.visible_bars(con, source, cid, "1m" if has_1m else scope.timeframe, as_of, trading_day=day_s,
                                session_type="RTH", start_utc=run["market_watermark_utc"]) if source else []
    market_wm = run["market_watermark_utc"]
    sub_i = 0
    state_json = run["state_json"]
    now = int(time.time())
    result = None
    caught_up = []

    def advance(upto: int, final: bool) -> list[dict]:
        """把成交模型推进到 upto。final 时再处理到期和截止后无行情的情形。"""
        nonlocal market_wm, sub_i
        before = _load_book(con, run_id)
        book, events = before, []
        while sub_i < len(subs) and subs[sub_i]["bar_end_utc"] <= upto:
            r = subs[sub_i]
            book, ev = fills.apply_bar(book, SubBar(r["bar_start_utc"], r["bar_end_utc"], r["open"], r["high"],
                                                    r["low"], r["close"]), fill_cfg)
            events += ev
            market_wm = r["bar_end_utc"]
            sub_i += 1
        if final:
            book, ev = fills.expire_orders(book, as_of)
            events += ev
            book, ev = fills.flatten_check(book, as_of)
            events += ev
        return _persist(con, run, scope, inst, cid, day_s, risk_cfg, before, book, events)

    if not pending:                                   # 没有新的信号 K：只维护订单簿和水位
        event_log = advance(as_of, final=True)
        cur = con.execute("UPDATE engine_runs SET watermark_utc=?, market_watermark_utc=?, version=version+1,"
                          " updated_at_utc=? WHERE run_id=? AND version=?",
                          (as_of, market_wm, now, run_id, run["version"]))
        if cur.rowcount != 1:
            raise ClockError(f"运行 {run_id} 已被其他进程更新")
        return {**base, "status": "NO_BAR", "events": event_log}

    for n, signal in enumerate(pending):
        last = n == len(pending) - 1
        # 1) 推进行情：到这根信号 K 收盘为止、此刻已可见的子 K
        event_log = advance(signal["bar_end_utc"], final=last)
        book = _load_book(con, run_id)
        # 这根 K 的"看得见的时点"：补算的旧 K 用它自己的可用时点，最新一根用真正的决策时点
        eff = as_of if last else min(as_of, max(signal["available_at_utc"], signal["bar_end_utc"]))

        # 2) 特征上下文（只用当时可见的数据）与策略决策
        hist = context.visible_bars(con, source, cid, scope.timeframe, eff, end_utc=signal["bar_start_utc"] + 1,
                                    limit=WARMUP_BARS, newest_first=True)[::-1]
        lv = context.reference_levels_asof(con, settings, scope.instrument_root, day, eff)
        closed = tuple(ClosedTrade(r["setup_id"] or "", r["exit_reason"], r["net_sign"]) for r in con.execute(
            "SELECT setup_id, exit_reason, net_sign FROM paper_trades WHERE run_id=? AND trading_day=?"
            " ORDER BY exit_time_utc, trade_id", (run_id, day_s)))
        ctx = features.build_context(
            scope=str(scope), instrument_root=scope.instrument_root, contract_id=cid, as_of_utc=eff,
            bars=[features.to_bar(r) for r in hist], rth_open_utc=rth_open, rth_close_utc=rth_close,
            day_ranges=context.rth_day_ranges_asof(con, settings, scope.instrument_root, eff, features.ADR_LOOKBACK_DAYS),
            hourly=[features.to_bar(r) for r in context.hourly_asof(con, source, cid, eff, features.HTF_BARS)],
            reference_levels={k: lv[k] for k in ("pdh", "pdl", "prev_rth_close", "onh", "onl")},
            closed_trades=closed, known_at_utc=signal["available_at_utc"])
        res = strategy.on_bar(ctx, StrategyState.from_json(state_json))
        state_json = res.next_state.to_json()

        # 3) 风控与挂单。冻结状态在这里（写锁之内、提交之前）重读。挂单的激活时刻是真正的决策时点。
        risk_status, block_reason, order_id = "NOT_EVALUATED", None, None
        paper_action = "MANAGE_ONLY" if book.position is not None else "NONE"
        if res.signal_status == "SETUP_VALID":
            intent = res.order_intent
            expires = signal["bar_end_utc"] + intent.ttl_bars * tf
            frozen = frozen_reason(con, scope, run["risk_domain"])
            if as_of >= expires:
                block_reason = "MISSED_LATE_SIGNAL"   # 来得太晚：不补挂
            elif as_of >= cutoff or signal["bar_end_utc"] >= cutoff:
                risk_status, block_reason = "BLOCKED", "AFTER_CUTOFF"
            elif frozen:
                risk_status, block_reason = "BLOCKED", frozen
            elif book.position is not None and book.position.side == intent.side:
                risk_status, block_reason = "BLOCKED", "POSITION_OPEN_SAME_SIDE"
            elif any(o.status in fills.WORKING for o in book.orders):
                risk_status, block_reason = "BLOCKED", "ORDER_ALREADY_WORKING"
            else:
                ref = intent.trigger_ticks if intent.trigger_ticks is not None else (
                    intent.limit_ticks if intent.limit_ticks is not None else signal["close"])
                dec = risk_mod.paper_risk(inst, max(1, abs(ref - intent.stop_ticks)), risk_cfg)
                risk_status, paper_action = dec.status, "QUEUE_ENTRY"
                order_id = "ord_" + _h(run_id, intent.setup_id)[:20]
                wo = WorkingOrder.from_intent(intent, order_id, as_of, expires, cutoff)
                con.execute(
                    "INSERT INTO paper_orders (order_id, scope, risk_domain, run_id, run_mode, setup_id, contract_id,"
                    " side, order_type, intent_json, status, version_tuple_json, created_at_utc, updated_at_utc,"
                    " signal_bar_end_utc, armed_at_utc, expires_at_utc, trigger_ticks, limit_ticks, stop_ticks,"
                    " target_json, quantity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order_id, str(scope), run["risk_domain"], run_id, run["run_mode"], intent.setup_id, cid,
                     intent.side, intent.order_type,
                     json.dumps({**intent.to_dict(), "flatten_at_utc": cutoff,
                                 "planned_risk_per_contract_usd": str(dec.risk_per_contract_usd)}, ensure_ascii=False,
                                default=str),
                     "ARMED", json.dumps(vt), now, now, signal["bar_end_utc"], wo.armed_at_utc, wo.expires_at_utc,
                     intent.trigger_ticks, intent.limit_ticks, intent.stop_ticks, json.dumps(intent.target.to_dict()),
                     dec.quantity))

        # 4) 扫描记录
        rows_upto = [r for r in day_rows if r["bar_end_utc"] <= signal["bar_end_utc"]]
        snap = _snapshot(con, scope, eff, day, cid, source, rows_upto, inst.roll_rule_version)
        event_id = "scan_" + _h(run_id, signal["bar_end_utc"])[:24]
        result = {**base, "status": "OK", "event_id": event_id, "snapshot_id": snap["snapshot_id"],
                  "signal_bar_end_utc": signal["bar_end_utc"], "signal_status": res.signal_status,
                  "risk_status": risk_status, "paper_action": paper_action, "block_reason": block_reason,
                  "order_id": order_id, "sub_bar_timeframe": "1m" if has_1m else scope.timeframe,
                  "catch_up": not last, "events": event_log,
                  "order_intent": None if res.order_intent is None else res.order_intent.to_dict(),
                  "decision_metadata": res.decision_metadata.to_dict()}
        result = json.loads(json.dumps(result, ensure_ascii=False, default=str))
        con.execute(
            "INSERT INTO scan_events (event_id, scope, strategy_family, run_mode, risk_domain, run_id, as_of_utc,"
            " snapshot_id, contract_id, signal_status, risk_status, paper_action, version_tuple_json,"
            " strategy_state_json, decision_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, str(scope), scope.strategy_family, run["run_mode"], run["risk_domain"], run_id, as_of,
             snap["snapshot_id"], cid, res.signal_status, risk_status, paper_action, json.dumps(vt),
             state_json, json.dumps(result, ensure_ascii=False), now))
        if not last:
            caught_up.append({k: result[k] for k in ("event_id", "signal_bar_end_utc", "signal_status",
                                                     "risk_status", "block_reason")})

    # 5) 策略状态与水位
    cur = con.execute(
        "UPDATE engine_runs SET watermark_utc=?, last_bar_end_utc=?, market_watermark_utc=?, state_json=?,"
        " version=version+1, updated_at_utc=? WHERE run_id=? AND version=?",
        (as_of, pending[-1]["bar_end_utc"], market_wm, state_json, now, run_id, run["version"]))
    if cur.rowcount != 1:
        raise ClockError(f"运行 {run_id} 已被其他进程更新")
    if caught_up:
        result["caught_up"] = caught_up
    return result


# ---------------------------------------------------------------- 回放
def replay(settings: Settings, con: sqlite3.Connection, scope: Scope, strategy, start_day: date, end_day: date, *,
           run_id: str | None = None, risk_cfg: RiskConfig | None = None, engine_cfg: EngineConfig | None = None,
           release_con: sqlite3.Connection | None = None, allow_real_data: bool = False) -> dict:
    """按 K 线收盘依次步进。决策时点取每根信号 K（第一个 revision）的可用时点加配置的延迟。

    真实数据的保护：只有显式允许、并且已配置开发可见截止日、回放区间不超过它时，才在非合成数据上回放。
    """
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    inst = settings.instrument(scope.instrument_root)
    days = sessions.trading_days(start_day, end_day)
    plan = []
    real = False
    for d in days:
        cid = contracts.active_contract(inst, d, settings.roll_rules)
        row = context.day_row(con, scope.instrument_root, d.isoformat())
        if row is None or row["source"] is None:
            continue
        real = real or not row["fixture"]
        for r in con.execute(
                "SELECT bar_end_utc, MIN(available_at_utc) a FROM bars WHERE source=? AND contract_id=? AND timeframe=?"
                " AND trading_day=? AND session_type='RTH' GROUP BY bar_start_utc ORDER BY bar_start_utc",
                (row["source"], cid, scope.timeframe, d.isoformat())):
            plan.append(max(r["a"], r["bar_end_utc"]) + engine_cfg.replay_activation_delay_seconds)
    if real:
        cut = settings.dev_visible_through
        if not allow_real_data or cut is None or end_day > cut:
            raise ReplayRefused(
                "拒绝在真实数据上回放：需要显式允许，并且先配置开发可见截止日（dev_visible_through），"
                "回放区间不得超过它。否则会污染以后的盲测数据。")
    run_id = create_run(con, scope, strategy, "historical_replay" if real else "fixture", run_id)
    counts = {"steps": 0, "setups": 0, "orders": 0}
    for as_of in sorted(set(plan)):
        r = step(settings, con, run_id, strategy, as_of, risk_cfg=risk_cfg, engine_cfg=engine_cfg,
                 release_con=release_con)
        counts["steps"] += r["status"] == "OK"
        counts["setups"] += r.get("signal_status") == "SETUP_VALID"
        counts["orders"] += r.get("order_id") is not None
    trades = con.execute("SELECT COUNT(*), COALESCE(SUM(pnl_ticks),0) FROM paper_trades WHERE run_id=?",
                         (run_id,)).fetchone()
    return {"run_id": run_id, "scope": str(scope), "run_mode": "historical_replay" if real else "fixture",
            "days": len(days), **counts, "trades": trades[0], "pnl_ticks": trades[1]}
