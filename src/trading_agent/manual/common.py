"""人工轨共用：品种与合约的解析（期货和股票类）、策略视图、当日最近的平仓。"""
from __future__ import annotations

import sqlite3
from datetime import date

from .. import contracts, context, sessions
from ..config import Scope, Settings
from ..strategies.base import Bar, FeatureContext, StrategyState


def is_equity(settings: Settings, inst) -> bool:
    """股票类品种：换月规则的种类是 none。没有合约月，合约标识就是代码本身。"""
    return (settings.roll_rules.get(inst.roll_rule_version) or {}).get("kind") == "none"


def unit(settings: Settings, inst) -> str:
    return "股" if is_equity(settings, inst) else "手"


def active_contract(settings: Settings, inst, day: date) -> str:
    return inst.root if is_equity(settings, inst) else contracts.active_contract(inst, day, settings.roll_rules)


def resolve_contract(settings: Settings, text: str, ref_date: date):
    """把来源里的合约或代码解析成 (contract_id, 品种)。股票类直接用代码；期货规范化成四位年份。"""
    code = str(text).strip().upper()
    inst = settings.instruments.get(code)
    if inst is not None and is_equity(settings, inst):
        return code, inst
    cid = contracts.normalize(code, ref_date)
    return cid, settings.instrument(contracts.parse(cid)[0])


def instrument_of(settings: Settings, contract_id: str):
    """已入库的 contract_id 对应的品种。"""
    inst = settings.instruments.get(contract_id)
    if inst is not None and is_equity(settings, inst):
        return inst
    return settings.instrument(contracts.parse(contract_id)[0])


def paper_run_id(scope: Scope | str) -> str:
    return f"paper:{scope}"


def strategy_view(settings: Settings, con: sqlite3.Connection, scope: Scope, strategy, now_utc: int,
                  max_age_bars: int, run_id: str | None = None):
    """该 scope 纸面运行的最新策略状态和一个最小的上下文。没有运行、状态过期时返回 None。

    返回 (state, ctx)。ctx 只用于一致性评估，里面是策略上次处理的那根 K。
    """
    r = con.execute("SELECT * FROM engine_runs WHERE run_id=?", (run_id or paper_run_id(scope),)).fetchone()
    if r is None or r["strategy_family"] != strategy.strategy_family or not r["last_bar_end_utc"]:
        return None
    if now_utc - r["last_bar_end_utc"] > max_age_bars * 300 + 300:
        return None
    end = r["last_bar_end_utc"]
    bar = Bar(end - 300, end, 0, 0, 0, 0)
    day = sessions.trading_day_at(end - 1)
    o, c = sessions.rth_bounds(day)
    cid = active_contract(settings, settings.instrument(scope.instrument_root), day)
    ctx = FeatureContext(scope=str(scope), instrument_root=scope.instrument_root, contract_id=cid, as_of_utc=now_utc,
                         bars=(bar,), rth_open_utc=o, rth_close_utc=c)
    return StrategyState.from_json(r["state_json"]), ctx


def last_closed(con: sqlite3.Connection, account_id: str, trading_day: str, before_utc: int | None = None):
    """账户当日最近一笔平仓（未隔离）。"""
    q = ("SELECT * FROM manual_trades WHERE account_id=? AND trading_day=? AND effect='CLOSE' AND quarantined=0"
         + (" AND executed_at_utc<=?" if before_utc is not None else "") + " ORDER BY executed_at_utc DESC, trade_id DESC LIMIT 1")
    args = (account_id, trading_day) + ((before_utc,) if before_utc is not None else ())
    return con.execute(q, args).fetchone()
