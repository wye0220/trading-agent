"""基线度量：从回放留下的账本和决策记录里算数。只度量，不产生任何信号或成交。

金额用 Decimal；比率和统计量是浮点，只用于报告。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter
from decimal import Decimal

from .. import sessions
from ..config import Scope, Settings
from ..engine import risk as risk_mod
from ..engine.config import RiskConfig

D0 = Decimal(0)


def _f(x) -> float | None:
    return None if x is None else float(x)


def _path(obj, dotted: str):
    for key in dotted.split("."):
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def _bucket(entry_utc: int, bounds: tuple[str, ...]) -> str:
    hm = sessions.et(entry_utc).strftime("%H:%M")
    label = None
    for i, b in enumerate(bounds):
        if hm >= b:
            label = f"{b}–{bounds[i + 1]}" if i + 1 < len(bounds) else f"{b} 之后"
    return label or f"{bounds[0]} 之前" if bounds else "全天"


# ---------------------------------------------------------------- 读取
def setup_metadata(con: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    """setup_id → 该信号的 decision_metadata（来自生成了订单的那条决策记录）。"""
    out = {}
    for (text,) in con.execute("SELECT decision_json FROM scan_events WHERE run_id=? AND signal_status='SETUP_VALID'",
                               (run_id,)):
        d = json.loads(text)
        meta = d.get("decision_metadata") or {}
        sid = meta.get("setup_id") or (d.get("order_intent") or {}).get("setup_id")
        if sid:
            out[sid] = {**meta, "_sub_bar_timeframe": d.get("sub_bar_timeframe")}
    return out


def load_trades(settings: Settings, con: sqlite3.Connection, scope: Scope, run_id: str, risk_cfg: RiskConfig,
                meta: dict[str, dict] | None = None, trading_day: str | None = None) -> list[dict]:
    """一次运行的已完结交易（可只取一个交易日），带事后度量：MAE／MFE、持仓时长、跳空超计划损失、每手计划风险。"""
    inst = settings.instrument(scope.instrument_root)
    tv = inst.tick_value_usd
    meta = meta or {}
    out = []
    rows = con.execute(
        "SELECT t.*, p.stop_ticks AS final_stop_ticks, o.intent_json, o.signal_bar_end_utc FROM paper_trades t"
        " LEFT JOIN paper_positions p ON p.position_id = t.trade_id"
        " LEFT JOIN paper_orders o ON o.order_id = p.order_id"
        " WHERE t.run_id=?" + (" AND t.trading_day=?" if trading_day else "")
        + " ORDER BY t.entry_time_utc, t.exit_time_utc, t.trade_id",
        (run_id, trading_day) if trading_day else (run_id,)).fetchall()
    for r in rows:
        d = 1 if r["side"] == "LONG" else -1
        day = con.execute("SELECT source FROM trading_days WHERE instrument_root=? AND trading_day=?",
                          (scope.instrument_root, r["trading_day"])).fetchone()
        source = day["source"] if day else None
        sub = "1m" if source and con.execute(
            "SELECT 1 FROM bars WHERE source=? AND contract_id=? AND timeframe='1m' AND trading_day=? AND derived=0"
            " LIMIT 1", (source, r["contract_id"], r["trading_day"])).fetchone() else scope.timeframe
        hi = lo = None
        if source:
            x = con.execute(
                "SELECT MAX(high), MIN(low) FROM bars b WHERE source=? AND contract_id=? AND timeframe=?"
                " AND bar_start_utc>=? AND bar_end_utc<=? AND revision=(SELECT MAX(revision) FROM bars x"
                " WHERE x.source=b.source AND x.contract_id=b.contract_id AND x.timeframe=b.timeframe"
                " AND x.bar_start_utc=b.bar_start_utc)",
                (source, r["contract_id"], sub, r["entry_time_utc"], r["exit_time_utc"])).fetchone()
            hi, lo = x[0], x[1]
        # 入场价和出场价本身一定在路径上；入场所在的那根子 K 先后不明，不计入
        hi = max(v for v in (hi, r["entry_ticks"], r["exit_ticks"]) if v is not None)
        lo = min(v for v in (lo, r["entry_ticks"], r["exit_ticks"]) if v is not None)
        mfe = (hi - r["entry_ticks"]) if d > 0 else (r["entry_ticks"] - lo)
        mae = (r["entry_ticks"] - lo) if d > 0 else (hi - r["entry_ticks"])
        flags = json.loads(r["flags_json"])
        gap_excess = 0
        if "GAP_THROUGH_STOP" in flags and r["final_stop_ticks"] is not None:
            gap_excess = max(0, d * (r["final_stop_ticks"] - r["exit_ticks"]) - risk_cfg.stop_slippage_ticks)
        intent = json.loads(r["intent_json"]) if r["intent_json"] else {}
        planned = intent.get("planned_risk_per_contract_usd")
        qty = r["quantity"]
        # 模型滑点：止损和截止强平的成交价已各让了配置的 tick 数。入场滑点配置为 0 时这里就是全部。
        slip_ticks = risk_cfg.stop_slippage_ticks if r["exit_reason"] in ("STOP", "FORCED_FLAT") else 0
        m = meta.get(r["setup_id"] or "", {})
        out.append({
            "trade_id": r["trade_id"], "trading_day": r["trading_day"], "contract_id": r["contract_id"],
            "setup_id": r["setup_id"], "side": r["side"], "quantity": qty,
            "entry_ticks": r["entry_ticks"], "entry_time_utc": r["entry_time_utc"],
            "exit_ticks": r["exit_ticks"], "exit_time_utc": r["exit_time_utc"], "exit_reason": r["exit_reason"],
            "initial_risk_ticks": r["initial_risk_ticks"], "pnl_ticks": r["pnl_ticks"],
            "gross_usd": Decimal(r["gross_usd"]), "fees_usd": Decimal(r["fees_usd"]), "net_usd": Decimal(r["net_usd"]),
            "r_initial": None if r["r_initial"] is None else Decimal(r["r_initial"]),
            "r_research": None if r["r_research"] is None else Decimal(r["r_research"]),
            "flags": flags, "holding_seconds": r["exit_time_utc"] - r["entry_time_utc"],
            "mae_ticks": mae, "mfe_ticks": mfe, "sub_bar_timeframe": sub,
            "gap_excess_ticks": gap_excess, "gap_excess_usd": gap_excess * tv * qty,
            "slippage_cost_usd": slip_ticks * tv * qty,
            "planned_risk_per_contract_usd": None if planned is None else Decimal(str(planned)),
            "metadata": {k: v for k, v in m.items() if not k.startswith("_")},
        })
    return out


# ---------------------------------------------------------------- 汇总
def max_drawdown(values) -> float:
    peak = cum = dd = 0.0
    for v in values:
        cum += float(v)
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _dist(xs) -> dict | None:
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return None
    return {"mean": statistics.fmean(xs), "median": statistics.median(xs), "max": max(xs), "min": min(xs)}


def summarize(trades: list[dict], days: list[str], *, tick_value: Decimal, research_unit: Decimal | None,
              stress_ticks_per_side: int = 1) -> dict:
    """一组交易在一组有效交易日上的汇总。days 是分母：零交易日计入。"""
    dayset = set(days)
    ts = [t for t in trades if t["trading_day"] in dayset]
    n = len(ts)
    net = sum((t["net_usd"] for t in ts), D0)
    gross = sum((t["gross_usd"] for t in ts), D0)
    fees = sum((t["fees_usd"] for t in ts), D0)
    wins = [t for t in ts if t["net_usd"] > 0]
    losses = [t for t in ts if t["net_usd"] < 0]
    win_sum = sum((t["net_usd"] for t in wins), D0)
    loss_sum = -sum((t["net_usd"] for t in losses), D0)
    by_day: dict[str, Decimal] = {d: D0 for d in days}
    cnt_day: Counter = Counter()
    for t in ts:
        by_day[t["trading_day"]] += t["net_usd"]
        cnt_day[t["trading_day"]] += 1
    daily_usd = [by_day[d] for d in days]
    daily_r = [float(x / research_unit) for x in daily_usd] if research_unit else None
    stress = 2 * stress_ticks_per_side * tick_value
    stress_net = net - sum((stress * t["quantity"] for t in ts), D0)
    flag_counts = Counter(f for t in ts for f in t["flags"])
    r_res = [t["r_research"] for t in ts if t["r_research"] is not None]
    r_ini = [t["r_initial"] for t in ts if t["r_initial"] is not None]
    return {
        "days": len(days), "trades": n, "no_trade_days": sum(1 for d in days if cnt_day[d] == 0),
        "trades_per_day": n / len(days) if days else None,
        "gross_usd": gross, "fees_usd": fees, "net_usd": net,
        "slippage_cost_usd": sum((t["slippage_cost_usd"] for t in ts), D0),
        "win_rate": len(wins) / n if n else None,
        "profit_factor": None if loss_sum == 0 else float(win_sum / loss_sum),     # 分母为 0：不可估
        "profit_factor_note": "不可估（没有亏损交易）" if n and loss_sum == 0 else None,
        "avg_win_usd": win_sum / len(wins) if wins else None,
        "avg_loss_usd": -loss_sum / len(losses) if losses else None,
        "net_r_per_trade": statistics.fmean(float(x) for x in r_res) if r_res else None,
        "net_r_initial_per_trade": statistics.fmean(float(x) for x in r_ini) if r_ini else None,
        "net_r_per_day": statistics.fmean(daily_r) if daily_r else None,
        "net_r_total": sum(daily_r) if daily_r else None,
        "daily_r_std": statistics.stdev(daily_r) if daily_r and len(daily_r) > 1 else None,
        "max_drawdown_usd": max_drawdown(daily_usd), "max_drawdown_r": max_drawdown(daily_r) if daily_r else None,
        "max_drawdown_trade_usd": max_drawdown(t["net_usd"] for t in ts),
        "mae_ticks": _dist(t["mae_ticks"] for t in ts), "mfe_ticks": _dist(t["mfe_ticks"] for t in ts),
        "mae_r_initial": _dist(t["mae_ticks"] / t["initial_risk_ticks"] for t in ts if t["initial_risk_ticks"] > 0),
        "mfe_r_initial": _dist(t["mfe_ticks"] / t["initial_risk_ticks"] for t in ts if t["initial_risk_ticks"] > 0),
        "holding_minutes": _dist(t["holding_seconds"] / 60 for t in ts),
        "exit_reasons": dict(Counter(t["exit_reason"] for t in ts)),
        "flags": dict(flag_counts),
        "ambiguous_bar_share": flag_counts.get("AMBIGUOUS_BAR", 0) / n if n else None,
        "ambiguous_entry_share": flag_counts.get("AMBIGUOUS_ENTRY", 0) / n if n else None,
        "gap_through_stop": {"trades": sum(1 for t in ts if "GAP_THROUGH_STOP" in t["flags"]),
                             "excess_ticks": sum(t["gap_excess_ticks"] for t in ts),
                             "excess_usd": sum((t["gap_excess_usd"] for t in ts), D0)},
        "coarse_sub_bar_trades": sum(1 for t in ts if t["sub_bar_timeframe"] != "1m"),
        "stress": {"extra_ticks_per_side": stress_ticks_per_side, "net_usd": stress_net,
                   "net_r_per_day": float(stress_net / research_unit) / len(days) if research_unit and days else None},
    }


def group_table(trades: list[dict], days: list[str], key, **kw) -> dict:
    """按 key(trade) 分组的小表。各组的分母都是同一组交易日。"""
    groups: dict[str, list] = {}
    dayset = set(days)
    for t in trades:
        if t["trading_day"] in dayset:
            k = key(t)
            groups.setdefault("（无）" if k is None else str(k), []).append(t)
    out = {}
    for k in sorted(groups):
        s = summarize(groups[k], days, **kw)
        out[k] = {x: s[x] for x in ("trades", "net_usd", "win_rate", "profit_factor", "net_r_per_trade", "net_r_total")}
    return out


def daily_series(trades: list[dict], days: list[str], research_unit: Decimal | None) -> dict:
    """逐日净 R（没有研究风险单位时为美元）和成交数，零交易日为 0。功效测算的输入。"""
    net = {d: D0 for d in days}
    cnt = {d: 0 for d in days}
    for t in trades:
        if t["trading_day"] in net:
            net[t["trading_day"]] += t["net_usd"]
            cnt[t["trading_day"]] += 1
    unit = research_unit or Decimal(1)
    return {"days": list(days), "net": [float(net[d] / unit) for d in days], "trades": [cnt[d] for d in days],
            "unit": "R" if research_unit else "USD"}


# ---------------------------------------------------------------- 风险规模
def risk_sized(trades: list[dict], days: list[str], risk_cfg: RiskConfig, **kw) -> dict:
    """按账户预算给固定手数的交易重新定手数。信号和成交路径与 1 手相同，所以只是重算，不需要再回放。

    账户限额未配置或未批准时不出数。当日净亏损达到日损上限或止损次数达到上限后，当日其余交易跳过。
    """
    acct = risk_cfg.account
    if not acct.configured:
        return {"status": "NOT_CONFIGURED", "reason": "账户限额没有填写（config/risk.yaml 的 account）"}
    if not acct.approved:
        return {"status": "NOT_CONFIGURED", "reason": "账户限额未批准（account.approved 为 false）"}
    dayset = set(days)
    scaled, skipped_size, skipped_halt = [], 0, 0
    state: dict[str, list] = {}
    for t in sorted((t for t in trades if t["trading_day"] in dayset), key=lambda t: (t["entry_time_utc"], t["trade_id"])):
        loss, stops = state.setdefault(t["trading_day"], [D0, 0])
        if loss >= acct.daily_loss_limit_usd or stops >= acct.max_stops_per_day:
            skipped_halt += 1
            continue
        risk = t["planned_risk_per_contract_usd"]
        base_qty = t["quantity"] or 1
        qty = 0 if risk is None or risk <= 0 else risk_mod.size_for_budget(
            risk, acct.budget_per_trade_usd, acct.max_contracts, acct.daily_loss_limit_usd - loss)
        if qty <= 0:
            skipped_size += 1
            continue
        k = Decimal(qty) / base_qty
        s = {**t, "quantity": qty, "gross_usd": t["gross_usd"] * k, "fees_usd": t["fees_usd"] * k,
             "net_usd": t["net_usd"] * k, "slippage_cost_usd": t["slippage_cost_usd"] * k,
             "gap_excess_usd": t["gap_excess_usd"] * k, "pnl_ticks": t["pnl_ticks"] * qty // base_qty,
             "r_research": None if t["r_research"] is None else t["r_research"] * k}
        scaled.append(s)
        st = state[t["trading_day"]]
        if s["net_usd"] < 0:
            st[0] += -s["net_usd"]
        st[1] += t["exit_reason"] == "STOP"
    return {"status": "OK", "summary": summarize(scaled, days, **kw),
            "limits": {"budget_per_trade_usd": acct.budget_per_trade_usd,
                       "daily_loss_limit_usd": acct.daily_loss_limit_usd, "max_contracts": acct.max_contracts,
                       "max_stops_per_day": acct.max_stops_per_day},
            "skipped_too_small": skipped_size, "skipped_after_halt": skipped_halt,
            "contracts": dict(Counter(t["quantity"] for t in scaled))}


def order_summary(con: sqlite3.Connection, run_id: str, days: list[str]) -> dict:
    """订单的去向：成交、到期、撤单及原因。"""
    dayset = set(days)
    status: Counter = Counter()
    ends: Counter = Counter()
    for r in con.execute("SELECT status, end_reason, signal_bar_end_utc FROM paper_orders WHERE run_id=?", (run_id,)):
        day = sessions.trading_day_at(r["signal_bar_end_utc"] - 1) if r["signal_bar_end_utc"] else None
        if day is None or day.isoformat() not in dayset:
            continue
        status[r["status"]] += 1
        if r["status"] != "FILLED":
            ends[f"{r['status']}:{r['end_reason'] or ''}"] += 1
    return {"orders": sum(status.values()), "by_status": dict(status), "unfilled_reasons": dict(ends)}
