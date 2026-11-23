"""人工轨共用：取策略视图、当日最近的平仓。"""
from __future__ import annotations

import sqlite3

from .. import contracts, context, sessions
from ..config import Scope, Settings
from ..strategies.base import Bar, FeatureContext, StrategyState


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
    cid = contracts.active_contract(settings.instrument(scope.instrument_root), day, settings.roll_rules)
    ctx = FeatureContext(scope=str(scope), instrument_root=scope.instrument_root, contract_id=cid, as_of_utc=now_utc,
                         bars=(bar,), rth_open_utc=o, rth_close_utc=c)
    return StrategyState.from_json(r["state_json"]), ctx


def last_closed(con: sqlite3.Connection, account_id: str, trading_day: str, before_utc: int | None = None):
    """账户当日最近一笔平仓（未隔离）。"""
    q = ("SELECT * FROM manual_trades WHERE account_id=? AND trading_day=? AND effect='CLOSE' AND quarantined=0"
         + (" AND executed_at_utc<=?" if before_utc is not None else "") + " ORDER BY executed_at_utc DESC, trade_id DESC LIMIT 1")
    args = (account_id, trading_day) + ((before_utc,) if before_utc is not None else ())
    return con.execute(q, args).fetchone()
