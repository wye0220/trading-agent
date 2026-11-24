"""风控：每手计划风险、手数、纸面交易结算、真实账户的预算、日损与停手。

纸面与真实账户是两个风险域：纸面以 scope、运行、交易日为键，固定手数，不经过账户预算门禁；
真实账户以账户、交易日为键，限制在所有策略之间共享。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from ..config import Instrument
from .. import db
from .config import RiskConfig


@dataclass(frozen=True)
class RiskDecision:
    status: str                    # PASS | BLOCKED
    reason_code: str | None
    quantity: int
    risk_per_contract_usd: Decimal
    detail: dict

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


@dataclass(frozen=True)
class AccountState:
    """真实账户某一交易日的风险状态。fetched_at_utc 是它所依据的账户快照的时点；None 表示未知。"""
    account_id: str
    trading_day: str
    realized_loss_usd: Decimal = Decimal(0)
    reserved_usd: Decimal = Decimal(0)
    stops: int = 0
    halted: bool = False
    version: int = 0
    fetched_at_utc: int | None = None
    complete: bool = True          # 仓位、止损、订单、成交数据是否齐全


def planned_risk_per_contract(inst: Instrument, stop_distance_ticks: int, cfg: RiskConfig) -> Decimal:
    """每手计划风险 = 止损 tick 数 × tick 价值 + 每手往返费用 + 滑点预算。"""
    if stop_distance_ticks <= 0:
        raise ValueError("止损距离必须为正")
    tv = inst.tick_value_usd
    return stop_distance_ticks * tv + cfg.round_trip_fee_usd + cfg.slippage_budget_ticks * tv


def size_for_budget(risk_per_contract: Decimal, budget: Decimal, max_contracts: int,
                    remaining_daily: Decimal) -> int:
    """手数 = floor(单笔预算 ÷ 每手风险)，再受最大手数和当日剩余风险约束。"""
    by_budget = int((budget / risk_per_contract).to_integral_value(ROUND_FLOOR))
    by_day = int((max(remaining_daily, Decimal(0)) / risk_per_contract).to_integral_value(ROUND_FLOOR))
    return max(0, min(by_budget, max_contracts, by_day))


def paper_risk(inst: Instrument, stop_distance_ticks: int, cfg: RiskConfig) -> RiskDecision:
    """纸面评测：固定手数，不经过账户预算门禁。"""
    r = planned_risk_per_contract(inst, stop_distance_ticks, cfg)
    return RiskDecision("PASS", None, cfg.paper_quantity, r, {"domain": "paper", "fees_configured": cfg.fees_configured})


def check_manual_plan(inst: Instrument, stop_distance_ticks: int | None, cfg: RiskConfig, state: AccountState | None,
                      now_utc: int, requested_quantity: int | None = None) -> RiskDecision:
    """人工计划的风险核验（纯函数）。按顺序检查，先命中的原因生效。"""
    zero = Decimal(0)
    lim = cfg.account

    def blocked(code, r=zero, **detail):
        return RiskDecision("BLOCKED", code, 0, r, {"domain": "account", **detail})

    if not lim.approved or not lim.configured:
        return blocked("BUDGET_NOT_APPROVED", approved=lim.approved, configured=lim.configured)
    if stop_distance_ticks is None:
        return blocked("NO_STOP")
    if (state is None or not state.complete or state.fetched_at_utc is None
            or now_utc - state.fetched_at_utc > lim.snapshot_max_age_seconds):
        age = None if state is None or state.fetched_at_utc is None else now_utc - state.fetched_at_utc
        return blocked("RISK_UNKNOWN_OR_STALE", snapshot_age_seconds=age,
                       max_age_seconds=lim.snapshot_max_age_seconds)
    r = planned_risk_per_contract(inst, stop_distance_ticks, cfg)
    if state.halted or state.stops >= lim.max_stops_per_day:
        return blocked("ACCOUNT_HALTED_TWO_STOPS", r, stops=state.stops)
    remaining = lim.daily_loss_limit_usd - state.realized_loss_usd - state.reserved_usd
    if state.realized_loss_usd >= lim.daily_loss_limit_usd:
        return blocked("DAILY_LIMIT_HIT", r, realized_loss_usd=str(state.realized_loss_usd))
    qty = size_for_budget(r, lim.budget_per_trade_usd, lim.max_contracts, remaining)
    detail = {"domain": "account", "remaining_daily_usd": str(remaining), "max_by_rules": qty}
    if qty == 0:
        by_budget = int((lim.budget_per_trade_usd / r).to_integral_value(ROUND_FLOOR))
        code = "MIN_SIZE_OVER_BUDGET" if by_budget == 0 else "DAILY_LIMIT_HIT"
        return RiskDecision("BLOCKED", code, 0, r, detail)
    if requested_quantity is not None and requested_quantity > qty:
        return RiskDecision("BLOCKED", "RISK_OVER_BUDGET", qty, r, {**detail, "requested": requested_quantity})
    return RiskDecision("PASS", None, qty if requested_quantity is None else requested_quantity, r, detail)


# ---------------------------------------------------------------- 纸面交易结算
def settle_trade(inst: Instrument, cfg: RiskConfig, *, side: str, quantity: int, entry_ticks: int, exit_ticks: int,
                 initial_stop_ticks: int, flags: tuple[str, ...] = ()) -> dict:
    """一笔已完结纸面交易的盈亏与 R。费用未配置按 0 计并打标记；研究风险单位未配置时只给按初始风险计的 R。"""
    d = 1 if side == "LONG" else -1
    tv = inst.tick_value_usd
    pnl_ticks = d * (exit_ticks - entry_ticks) * quantity
    gross = pnl_ticks * tv
    fees = cfg.round_trip_fee_usd * quantity
    net = gross - fees
    risk_ticks = d * (entry_ticks - initial_stop_ticks)
    out_flags = list(flags)
    if not cfg.fees_configured:
        out_flags.append("FEES_NOT_CONFIGURED")
    r_initial = None if risk_ticks <= 0 else net / (risk_ticks * tv * quantity)
    r_research = None
    if cfg.research_risk_unit_usd is None:
        out_flags.append("RESEARCH_RISK_UNIT_NOT_SET")
    else:
        r_research = net / cfg.research_risk_unit_usd
    return {"pnl_ticks": pnl_ticks, "gross_usd": gross, "fees_usd": fees, "net_usd": net,
            "net_sign": (net > 0) - (net < 0), "initial_risk_ticks": risk_ticks,
            "r_initial": r_initial, "r_research": r_research, "flags": tuple(dict.fromkeys(out_flags))}


# ---------------------------------------------------------------- 真实账户：持久化
def account_state(con: sqlite3.Connection, account_id: str, trading_day: str, *, fetched_at_utc: int | None = None,
                  complete: bool = True) -> AccountState:
    r = con.execute("SELECT * FROM account_risk_days WHERE account_id=? AND trading_day=?",
                    (account_id, trading_day)).fetchone()
    if r is None:
        return AccountState(account_id, trading_day, fetched_at_utc=fetched_at_utc, complete=complete)
    return AccountState(account_id, trading_day, Decimal(r["realized_loss_usd"]), Decimal(r["reserved_usd"]),
                        r["stops"], bool(r["halted"]), r["version"], fetched_at_utc, complete)


def _write_state(con, st: AccountState, *, realized, reserved, stops, halted):
    now = int(time.time())
    if st.version == 0:
        try:
            con.execute("INSERT INTO account_risk_days VALUES (?,?,?,?,?,?,1,?)",
                        (st.account_id, st.trading_day, str(realized), str(reserved), stops, int(halted), now))
        except sqlite3.IntegrityError:
            raise db.StaleVersion(f"账户 {st.account_id} {st.trading_day} 的风险状态已被其他进程创建") from None
        return
    cur = con.execute(
        "UPDATE account_risk_days SET realized_loss_usd=?, reserved_usd=?, stops=?, halted=?, version=version+1,"
        " updated_at_utc=? WHERE account_id=? AND trading_day=? AND version=?",
        (str(realized), str(reserved), stops, int(halted), now, st.account_id, st.trading_day, st.version))
    if cur.rowcount != 1:
        raise db.StaleVersion(f"账户 {st.account_id} {st.trading_day} 的风险状态版本不是 {st.version}")


def record_account_exit(con: sqlite3.Connection, cfg: RiskConfig, *, account_id: str, trading_day: str,
                        source_id: str, exit_reason: str, net_usd: Decimal, strategy_family: str | None = None) -> bool:
    """登记真实账户的一笔已完结交易。按 source_id（券商成交或订单 ID）去重：重复导入返回 False，不改状态。

    止损出场计 1 次；达到上限后当日停手。停手之前，只有净盈利的交易把计数清零。
    调用方负责事务（with con:）。
    """
    try:
        con.execute("INSERT INTO account_risk_events VALUES (?,?,?,?,?,?,?)",
                    (account_id, source_id, trading_day, exit_reason, str(net_usd), strategy_family, int(time.time())))
    except sqlite3.IntegrityError:
        return False
    st = account_state(con, account_id, trading_day)
    stops, halted = st.stops, st.halted
    if not halted:
        if exit_reason == "STOP":
            stops += 1
            halted = stops >= cfg.account.max_stops_per_day
        elif net_usd > 0:
            stops = 0
    realized = st.realized_loss_usd - net_usd           # 亏损使它增加，盈利使它减少
    _write_state(con, st, realized=realized, reserved=st.reserved_usd, stops=stops, halted=halted)
    return True


def reserve(con: sqlite3.Connection, inst: Instrument, cfg: RiskConfig, *, account_id: str, trading_day: str,
            plan_id: str, stop_distance_ticks: int, quantity: int, expected_version: int, now_utc: int,
            fetched_at_utc: int | None, complete: bool = True, strategy_family: str | None = None) -> RiskDecision:
    """核验并预留人工计划的风险，在同一个事务里完成。

    expected_version 是调用方做预检时读到的账户状态版本；与库中不一致时抛 StaleVersion，状态不变。
    同一个 plan_id 重复预留是空操作。
    """
    if con.in_transaction:
        raise RuntimeError("reserve 需要自己开事务；调用前请先提交或回滚")
    con.execute("BEGIN IMMEDIATE")              # 先拿写锁，再读状态：核验和预留之间不会被别的写入插进来
    try:
        done = con.execute("SELECT quantity, amount_usd FROM risk_reservations WHERE plan_id=? AND status='RESERVED'",
                           (plan_id,)).fetchone()
        if done:
            con.rollback()
            return RiskDecision("PASS", None, done["quantity"], Decimal(done["amount_usd"]) / done["quantity"],
                                {"domain": "account", "idempotent": True})
        st = account_state(con, account_id, trading_day, fetched_at_utc=fetched_at_utc, complete=complete)
        if st.version != expected_version:
            raise db.StaleVersion(f"账户风险状态已变化：预检时版本 {expected_version}，现在 {st.version}")
        dec = check_manual_plan(inst, stop_distance_ticks, cfg, st, now_utc, quantity)
        if not dec.passed:
            con.rollback()
            return dec
        amount = dec.risk_per_contract_usd * quantity
        _write_state(con, st, realized=st.realized_loss_usd, reserved=st.reserved_usd + amount, stops=st.stops,
                     halted=st.halted)
        now = int(time.time())
        con.execute("INSERT INTO risk_reservations VALUES (?,?,?,?,?,?,'RESERVED',?,?)",
                    (plan_id, account_id, trading_day, strategy_family, str(amount), quantity, now, now))
        con.commit()
        return dec
    except BaseException:
        con.rollback()
        raise


def release(con: sqlite3.Connection, plan_id: str) -> bool:
    """释放一份预留（计划取消、过期或成交后转为实际仓位）。"""
    with con:
        r = con.execute("SELECT * FROM risk_reservations WHERE plan_id=? AND status='RESERVED'", (plan_id,)).fetchone()
        if r is None:
            return False
        st = account_state(con, r["account_id"], r["trading_day"])
        _write_state(con, st, realized=st.realized_loss_usd, reserved=st.reserved_usd - Decimal(r["amount_usd"]),
                     stops=st.stops, halted=st.halted)
        con.execute("UPDATE risk_reservations SET status='RELEASED', updated_at_utc=? WHERE plan_id=?",
                    (int(time.time()), plan_id))
        return True
