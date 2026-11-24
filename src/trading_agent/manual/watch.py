"""盘中只读纪律提醒。按间隔读取账户快照，评估规则，写提醒。只陈述事实和规则，不给交易指令。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from decimal import Decimal
from typing import Callable

from .. import sessions
from ..config import Scope, Settings
from ..engine import risk as risk_mod
from ..engine.config import RiskConfig
from ..strategies.base import TraderPlan
from . import plans
from .broker import STOP_KINDS, AccountSnapshot, BrokerReadError, BrokerReadOnly
from . import common
from .common import strategy_view
from .config import LEVELS, ManualConfig

MIN_STALE_SECONDS = 120


def stale_after(interval_seconds: int) -> int:
    """数据过期阈值：max(2 × 轮询间隔, 120) 秒。"""
    return max(2 * interval_seconds, MIN_STALE_SECONDS)


def resolve_interval(watch_cfg: dict, override: int | None = None) -> int:
    v = watch_cfg["poll_interval_seconds"] if override is None else override
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"轮询间隔必须是正整数秒：{v!r}")
    return v


# ---------------------------------------------------------------- 状态
def _get(con, account_id: str, key: str, default=None):
    r = con.execute("SELECT value_json FROM watch_state WHERE account_id=? AND key=?", (account_id, key)).fetchone()
    return default if r is None else json.loads(r[0])


def _put(con, account_id: str, key: str, value, now: int):
    con.execute("INSERT INTO watch_state VALUES (?,?,?,?) ON CONFLICT(account_id, key) DO UPDATE SET"
                " value_json=excluded.value_json, updated_at_utc=excluded.updated_at_utc",
                (account_id, key, json.dumps(value), now))


def heartbeat(con: sqlite3.Connection, account_id: str) -> dict | None:
    return _get(con, account_id, "heartbeat")


def _protective_stop(snap: AccountSnapshot, pos):
    """保护止损单：同合约、方向相反、止损类型、数量不小于持仓。返回最贴近的那一张。"""
    want = "SELL" if pos.quantity > 0 else "BUY"
    c = [o for o in snap.open_orders if o.contract_id == pos.contract_id and o.side == want and o.kind in STOP_KINDS
         and o.quantity >= abs(pos.quantity) and o.price_ticks is not None]
    if not c:
        return None
    return max(c, key=lambda o: o.price_ticks) if pos.quantity > 0 else min(c, key=lambda o: o.price_ticks)


def _alert(con, cfg: ManualConfig, snap: AccountSnapshot, snapshot_id: str, rule: str, key: str, message: str,
           now: int, plan_id: str | None = None, **detail) -> dict | None:
    """写一条提醒。同一持仓同一规则在静默窗口内不重复，返回 None。"""
    last = con.execute("SELECT created_at_utc FROM discipline_alerts WHERE account_id=? AND rule=? AND position_key=?"
                       " ORDER BY created_at_utc DESC LIMIT 1", (snap.account_id, rule, key)).fetchone()
    if last is not None and now - last[0] < cfg.quiet_window_minutes * 60:
        return None
    level = LEVELS.get(rule, "WARN")
    aid = "da_" + hashlib.sha256(f"{snap.account_id}|{rule}|{key}|{now}".encode()).hexdigest()[:24]
    day = sessions.trading_day_at(now).isoformat()
    con.execute(
        "INSERT INTO discipline_alerts (alert_id, account_id, rule, plan_id, broker_snapshot_id, detail_json,"
        " created_at_utc, trading_day, position_key, level, message) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (aid, snap.account_id, rule, plan_id, snapshot_id, json.dumps(detail, ensure_ascii=False, default=str), now,
         day, key, level, message))
    con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, created_at_utc) VALUES (?,?,?,?,?,?)",
                (level, rule, None, message, json.dumps({"alert_id": aid, "account_id": snap.account_id,
                                                         "snapshot_id": snapshot_id}), now))
    return {"alert_id": aid, "rule": rule, "level": level, "position_key": key, "message": message,
            "plan_id": plan_id, "snapshot_id": snapshot_id, "data_as_of_utc": sessions.iso(snap.fetched_at_utc)}


def _resolve(con, account_id: str, rule: str, key: str, now: int):
    con.execute("UPDATE discipline_alerts SET resolved_at_utc=? WHERE account_id=? AND rule=? AND position_key=?"
                " AND resolved_at_utc IS NULL", (now, account_id, rule, key))


def _active_plan(con, account_id: str, contract_id: str, side: str, now: int) -> str | None:
    for r in con.execute("SELECT plan_id FROM trader_plans WHERE account_id=? AND contract_id=? AND side=?"
                         " AND status IN ('ACTIVE','PARTIALLY_FILLED','FILLED')", (account_id, contract_id, side)):
        if plans.was_active_at(con, r["plan_id"], now) or con.execute(
                "SELECT status FROM trader_plans WHERE plan_id=?", (r["plan_id"],)).fetchone()[0] == "FILLED":
            return r["plan_id"]
    return None


# ---------------------------------------------------------------- 一轮
def poll_once(settings: Settings, con: sqlite3.Connection, adapter: BrokerReadOnly, account_id: str, *,
              scope: Scope, strategy, risk_cfg: RiskConfig, manual_cfg: ManualConfig, interval_seconds: int,
              now_utc: int | None = None, notify: Callable[[dict], None] | None = None) -> dict:
    """采集一次并评估规则。数据过期或读取失败时只发数据告警，不评估任何规则。"""
    now = int(time.time()) if now_utc is None else now_utc
    limit = stale_after(interval_seconds)
    grace = interval_seconds
    out = {"account_id": account_id, "as_of_utc": sessions.iso(now), "interval_seconds": interval_seconds,
           "stale_after_seconds": limit, "status": "OK", "alerts": [], "unknown": [], "snapshot_id": None}
    try:
        snap = adapter.fetch(account_id)
        error = None
    except BrokerReadError as e:
        snap, error = None, str(e)
    stale = snap is not None and now - snap.fetched_at_utc > limit
    with con:
        _put(con, account_id, "heartbeat", {"at_utc": now, "ok": snap is not None and not stale,
                                            "interval_seconds": interval_seconds}, now)
        if snap is not None:
            sid = "bs_" + hashlib.sha256(f"{account_id}|{snap.fetched_at_utc}|{snap.digest()}".encode()).hexdigest()[:24]
            con.execute("INSERT OR IGNORE INTO broker_snapshots VALUES (?,?,?,?,?,?,?,?)",
                        (sid, account_id, snap.fetched_at_utc, snap.source, json.dumps(snap.to_json()["positions"]),
                         json.dumps(snap.to_json()["open_orders"]), json.dumps(snap.to_json()["fills_today"]),
                         snap.digest()))
            out["snapshot_id"] = sid
            out["data_as_of_utc"] = sessions.iso(snap.fetched_at_utc)
        if snap is None or stale:
            msg = (f"账户 {account_id} 的券商数据读取失败：{error}" if snap is None else
                   f"账户 {account_id} 的券商数据已过期：数据时点 {sessions.iso(snap.fetched_at_utc)}，"
                   f"已过去 {now - snap.fetched_at_utc} 秒，阈值 {limit} 秒")
            con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, created_at_utc)"
                        " VALUES ('WARN','BROKER_DATA_STALE',NULL,?,?,?)",
                        (msg + "。本轮不评估任何规则。", json.dumps({"account_id": account_id}), now))
            out.update(status="STALE" if snap is not None else "READ_FAILED", message=msg)
            return out
        fired = _evaluate(settings, con, snap, out["snapshot_id"], scope, strategy, risk_cfg, manual_cfg, grace, now, out)
    out["alerts"] = fired
    for a in fired:
        (notify or (lambda _a: None))(a)
    return out


def _evaluate(settings, con, snap: AccountSnapshot, sid: str, scope, strategy, risk_cfg, cfg: ManualConfig,
              grace: int, now: int, out: dict) -> list[dict]:
    acct = snap.account_id
    fired: list[dict] = []
    prev = _get(con, acct, "positions", {})
    naked = _get(con, acct, "naked_since", {})
    stops = _get(con, acct, "stops", {})
    cur, new_naked, new_stops = {}, {}, {}
    day = sessions.trading_day_at(now).isoformat()
    on = cfg.rules

    def add(a):
        if a:
            fired.append(a)

    for pos in snap.positions:
        key = f"{acct}:{pos.contract_id}:{pos.side}"
        cur[key] = pos.quantity
        before = prev.get(key, 0)
        inst = common.instrument_of(settings, pos.contract_id)
        equity = common.is_equity(settings, inst)       # 股票类没有正式策略：不拿别的品种的策略状态来评价
        tick = inst.tick_size
        px = lambda t: str(t * tick)          # noqa: E731
        fact = f"{pos.contract_id} {'多' if pos.quantity > 0 else '空'}仓 {abs(pos.quantity)} {common.unit(settings, inst)}"
        stop = _protective_stop(snap, pos)

        # NAKED_POSITION：持仓没有保护止损单，且超过宽限时间
        if stop is None:
            since = naked.get(key, now)
            new_naked[key] = since
            if on["NAKED_POSITION"] and now - since >= grace:
                add(_alert(con, cfg, snap, sid, "NAKED_POSITION", key,
                           f"{fact}，已 {now - since} 秒没有对应的保护止损单（规则 NAKED_POSITION，宽限 {grace} 秒）。",
                           now, _active_plan(con, acct, pos.contract_id, pos.side, now), since_utc=since))
        else:
            _resolve(con, acct, "NAKED_POSITION", key, now)

        # STOP_MOVED_AWAY：同一张止损单向不利方向移动；或止损单消失而持仓仍在
        if on["STOP_MOVED_AWAY"]:
            old = stops.get(key)
            d = 1 if pos.quantity > 0 else -1
            if stop is not None and old and old["order_id"] == stop.order_id and d * (stop.price_ticks - old["price"]) < 0:
                add(_alert(con, cfg, snap, sid, "STOP_MOVED_AWAY", key,
                           f"{fact}，止损单 {stop.order_id} 从 {px(old['price'])} 被改到 {px(stop.price_ticks)}，"
                           "离入场更远（规则 STOP_MOVED_AWAY）。", now, None, from_ticks=old["price"],
                           to_ticks=stop.price_ticks))
            elif stop is None and old and before:
                add(_alert(con, cfg, snap, sid, "STOP_MOVED_AWAY", key,
                           f"{fact}，原有的止损单 {old['order_id']}（{px(old['price'])}）已不在挂单中，而持仓仍在"
                           "（规则 STOP_MOVED_AWAY）。", now, None, removed_order_id=old["order_id"]))
        if stop is not None:
            new_stops[key] = {"order_id": stop.order_id, "price": stop.price_ticks}

        opened = before == 0
        increased = abs(pos.quantity) > abs(before)
        plan_id = _active_plan(con, acct, pos.contract_id, pos.side, now)

        # UNPLANNED_ENTRY：新出现的持仓找不到相符的已激活计划
        if on["UNPLANNED_ENTRY"] and opened and plan_id is None:
            add(_alert(con, cfg, snap, sid, "UNPLANNED_ENTRY", key,
                       f"新出现 {fact}，没有找到相符的已激活计划（规则 UNPLANNED_ENTRY）。", now))
        elif plan_id is not None:
            _resolve(con, acct, "UNPLANNED_ENTRY", key, now)

        # REENTRY_IN_COOLDOWN：止损出场后的冷却时间内同方向再开仓
        if on["REENTRY_IN_COOLDOWN"] and opened:
            last = con.execute(
                "SELECT executed_at_utc, trade_id FROM manual_trades WHERE account_id=? AND contract_id=? AND side=?"
                " AND effect='CLOSE' AND exit_kind='STOP' AND quarantined=0 AND executed_at_utc<=?"
                " ORDER BY executed_at_utc DESC LIMIT 1", (acct, pos.contract_id, pos.side, now)).fetchone()
            if last is not None and now - last[0] <= cfg.cooldown_minutes * 60:
                st = risk_mod.account_state(con, acct, day)
                view = None if equity else strategy_view(settings, con, scope, strategy, now, cfg.strategy_state_max_age_bars)
                new_setup = None
                if view is not None:
                    sig = view[0].data.get("last_signal")
                    new_setup = bool(sig) and (sig["d"] > 0) == (pos.quantity > 0)
                add(_alert(con, cfg, snap, sid, "REENTRY_IN_COOLDOWN", key,
                           f"止损出场后 {now - last[0]} 秒，同方向再次出现 {fact}（规则 REENTRY_IN_COOLDOWN，冷却 "
                           f"{cfg.cooldown_minutes} 分钟）。新的有效 setup：{'未知' if new_setup is None else ('有' if new_setup else '无')}；"
                           f"已激活计划：{'有' if plan_id else '无'}；账户停手：{'是' if st.halted else '否'}。",
                           now, plan_id, stop_trade_id=last[1], new_setup=new_setup, active_plan=plan_id,
                           account_halted=st.halted))

        # COUNTERTREND_ADD：加仓，而策略对这个方向的评估为逆势
        if on["COUNTERTREND_ADD"] and increased and not opened:
            view = None if equity else strategy_view(settings, con, scope, strategy, now, cfg.strategy_state_max_age_bars)
            if view is None:
                out["unknown"].append({"rule": "COUNTERTREND_ADD", "position_key": key,
                                       "why": "该品种没有正式策略" if equity else "没有可用的策略状态"})
            else:
                res = strategy.evaluate_pretrade_alignment(
                    view[1], view[0], TraderPlan(pos.side, pos.avg_price_ticks or 0, None, None, abs(pos.quantity),
                                                 pos.contract_id))
                if res.alignment == "DEVIATES" and "COUNTERTREND_BEFORE_CONFIRMATION" in res.flags:
                    add(_alert(con, cfg, snap, sid, "COUNTERTREND_ADD", key,
                               f"{fact}（由 {abs(before)} {common.unit(settings, inst)}增加），而策略当前的结构与这个方向相反"
                               "（规则 COUNTERTREND_ADD）。", now, plan_id, evidence=dict(res.evidence)))

    # 已不存在的持仓：解除它们的提醒
    for key in set(prev) - set(cur):
        for rule in ("NAKED_POSITION", "STOP_MOVED_AWAY", "UNPLANNED_ENTRY", "REENTRY_IN_COOLDOWN", "COUNTERTREND_ADD"):
            _resolve(con, acct, rule, key, now)

    # DAILY_LOSS_NEAR_LIMIT：已实现加未实现亏损达到日损上限的一定比例
    lim = risk_cfg.account.daily_loss_limit_usd
    unreal = [p.unrealized_usd for p in snap.positions]
    if on["DAILY_LOSS_NEAR_LIMIT"] and lim is not None and lim > 0 and all(u is not None for u in unreal):
        st = risk_mod.account_state(con, acct, day)
        loss = st.realized_loss_usd - sum(unreal, Decimal(0))
        key = f"{acct}:daily"
        if loss >= lim * Decimal(str(cfg.daily_loss_warn_fraction)):
            add(_alert(con, cfg, snap, sid, "DAILY_LOSS_NEAR_LIMIT", key,
                       f"账户 {acct} 当日已实现加未实现亏损 {loss} 美元，达到日损上限 {lim} 美元的 "
                       f"{int(cfg.daily_loss_warn_fraction * 100)}%（规则 DAILY_LOSS_NEAR_LIMIT）。", now,
                       loss_usd=str(loss), limit_usd=str(lim)))
        else:
            _resolve(con, acct, "DAILY_LOSS_NEAR_LIMIT", key, now)
    elif on["DAILY_LOSS_NEAR_LIMIT"]:
        out["unknown"].append({"rule": "DAILY_LOSS_NEAR_LIMIT",
                               "why": "日损上限未配置" if lim is None else "快照没有未实现盈亏"})

    _put(con, acct, "positions", cur, now)
    _put(con, acct, "naked_since", new_naked, now)
    _put(con, acct, "stops", new_stops, now)
    return fired


def run_loop(poll: Callable[[], dict], interval_seconds: int, *, iterations: int | None = None,
             sleep: Callable[[float], None] = time.sleep, emit: Callable[[dict], None] = lambda r: None,
             interval_fn: Callable[[], int] | None = None) -> int:
    """按间隔循环调用 poll。iterations 为 None 时一直运行。返回执行的轮数。

    interval_fn 给了时每轮重读间隔（控制通道的 watch-interval 下一轮起生效）。
    """
    n = 0
    while iterations is None or n < iterations:
        emit(poll())
        n += 1
        if iterations is None or n < iterations:
            sleep(interval_fn() if interval_fn else interval_seconds)
    return n
