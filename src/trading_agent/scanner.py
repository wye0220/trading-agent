"""scan：数据校验 + 策略 + 纸面账本。盘中用常驻的纸面运行；历史用一次性的运行把当日回放到决策时点。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from . import contracts, context, sessions
from .config import Scope, Settings
from .engine import runner
from .engine.config import EngineConfig, RiskConfig, load_engine, load_risk
from .manual.common import paper_run_id
from .manual.config import load_news_windows
from .scan import DISCLAIMER, _session_for, run_scan

STRATEGY_NOT_RUN = "STRATEGY_NOT_RUN"


def _ensure_run(con, scope: Scope, strategy, run_id: str, mode: str) -> sqlite3.Row:
    r = con.execute("SELECT * FROM engine_runs WHERE run_id=?", (run_id,)).fetchone()
    if r is None:
        runner.create_run(con, scope, strategy, mode, run_id)
        r = runner.get_run(con, run_id)
    return r


def _set_data_block(con, scope: Scope, risk_domain: str, blocked: bool, reason: str | None, as_of: int):
    """把数据校验的结论写到该运行的风险域上，引擎提交订单前会重读它。"""
    with con:
        con.execute(
            "INSERT INTO scope_data_status VALUES (?,?,?,?,?,1,?) ON CONFLICT(scope, risk_domain) DO UPDATE SET"
            " blocked=excluded.blocked, reason=excluded.reason, since_utc=excluded.since_utc, version=version+1,"
            " updated_at_utc=excluded.updated_at_utc",
            (str(scope), risk_domain, int(blocked), reason, as_of if blocked else None, int(time.time())))


def _position(con, run_id: str) -> dict | None:
    p = con.execute("SELECT * FROM paper_positions WHERE run_id=? AND status='OPEN'", (run_id,)).fetchone()
    return None if p is None else {"side": p["side"], "quantity": p["quantity"], "entry_ticks": p["entry_ticks"],
                                   "stop_ticks": p["stop_ticks"], "target_ticks": p["target_ticks"],
                                   "flags": json.loads(p["flags_json"])}


def _working_order(con, run_id: str) -> dict | None:
    o = con.execute("SELECT * FROM paper_orders WHERE run_id=? AND status IN ('ARMED','TRIGGERED')"
                    " ORDER BY armed_at_utc DESC LIMIT 1", (run_id,)).fetchone()
    return None if o is None else {"order_id": o["order_id"], "side": o["side"], "status": o["status"],
                                   "trigger_ticks": o["trigger_ticks"], "limit_ticks": o["limit_ticks"],
                                   "stop_ticks": o["stop_ticks"], "expires_at_utc": o["expires_at_utc"]}


def _news(settings: Settings, as_of: int) -> dict:
    windows = load_news_windows(settings.project_root)
    if not windows:
        return {"available": False, "active": [], "upcoming": []}
    return {"available": True,
            "active": [w for w in windows if w["start_utc"] <= as_of < w["end_utc"]],
            "upcoming": [w for w in windows if as_of < w["start_utc"] <= as_of + 4 * 3600]}


def _merge(settings: Settings, con, scope: Scope, validation: dict, step: dict | None, run_id: str | None,
           note: str | None) -> dict:
    as_of = sessions.parse_utc(validation["as_of_utc"])
    data_error = validation["signal_status"] == "DATA_ERROR"
    meta = (step or {}).get("decision_metadata") or {}
    out = {
        "scope": str(scope), "as_of_utc": validation["as_of_utc"], "run_id": run_id,
        "signal_status": "DATA_ERROR" if data_error else (step or {}).get("signal_status", "WAIT"),
        "risk_status": (step or {}).get("risk_status", "NOT_EVALUATED"),
        "paper_action": (step or {}).get("paper_action", "NONE"),
        "block_reason": (step or {}).get("block_reason"),
        "order_id": (step or {}).get("order_id"), "event_id": (step or {}).get("event_id"),
        "trading_day": validation["trading_day"], "contract_id": validation["contract_id"],
        "session": "RTH" if validation["in_rth"] else "RTH 之外", "source": validation["source"],
        "snapshot_id": validation["snapshot_id"], "fixture": validation["fixture"],
        "latest_bar": validation["latest_bar"], "bar_count": validation["bar_count"],
        "problems": validation["problems"], "degraded_day": validation["degraded_day"],
        "reference_levels": validation["reference_levels"], "adr_valid_days": validation["adr_valid_days"],
        "version_tuple": (step or {}).get("version_tuple") or validation["version_tuple"],
        "order_intent": (step or {}).get("order_intent"), "decision_metadata": meta or None,
        "strategy_ran": step is not None and step.get("status") in ("OK", "ALREADY_PROCESSED") and bool(meta),
        "strategy_note": note, "events": (step or {}).get("events", []),
        "paper_position": _position(con, run_id) if run_id else None,
        "working_order": _working_order(con, run_id) if run_id else None,
        "news_windows": _news(settings, as_of),
    }
    out["report"] = render(settings, out)
    return out


def live_scan(settings: Settings, con: sqlite3.Connection, scope: Scope, strategy, *, now_utc: int | None = None,
              risk_cfg: RiskConfig | None = None, engine_cfg: EngineConfig | None = None) -> dict:
    """盘中扫描。先校验数据；有异常时不产生新信号，已有的纸面仓位照常管理。"""
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    now = int(time.time()) if now_utc is None else now_utc
    validation = run_scan(settings, con, scope, now_utc=now, record_event=False)
    run_id = paper_run_id(scope)
    run = _ensure_run(con, scope, strategy, run_id, "paper")
    bad = validation["signal_status"] == "DATA_ERROR"
    _set_data_block(con, scope, run["risk_domain"], bad, validation["block_reason"], now)
    step = runner.step(settings, con, run_id, strategy, max(now, run["watermark_utc"]), risk_cfg=risk_cfg,
                       engine_cfg=engine_cfg)
    return _merge(settings, con, scope, validation, step, run_id, None)


def historical_scan(settings: Settings, con: sqlite3.Connection, scope: Scope, strategy, as_of_utc: int, *,
                    risk_cfg: RiskConfig | None = None, engine_cfg: EngineConfig | None = None,
                    allow_real_data: bool = False) -> dict:
    """历史扫描：开一个一次性的运行，把当日回放到 as_of。真实数据在没有划定开发边界时只做数据校验。"""
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    validation = run_scan(settings, con, scope, as_of_utc, record_event=False)
    day, _ = _session_for(as_of_utc)
    row = context.day_row(con, scope.instrument_root, day.isoformat())
    if row is None or row["source"] is None:
        return _merge(settings, con, scope, validation, None, None, "该交易日没有数据，策略没有运行")
    if not row["fixture"]:
        cut = settings.dev_visible_through
        if not allow_real_data or cut is None or day > cut:
            return _merge(settings, con, scope, validation, None, None,
                          "策略没有运行：这是真实数据，而开发可见截止日（dev_visible_through）没有设置、没有显式允许，"
                          "或该交易日晚于截止日。只返回数据校验结果，以免污染以后的盲测数据。")
    cid = contracts.active_contract(settings.instrument(scope.instrument_root), day, settings.roll_rules)
    run_id = "hscan_" + hashlib.sha256(f"{scope}|{as_of_utc}|{strategy.version}|{risk_cfg.version}".encode()).hexdigest()[:20]
    _ensure_run(con, scope, strategy, run_id, "fixture" if row["fixture"] else "historical_replay")
    step = None
    plan = [max(r["a"], r["bar_end_utc"]) + engine_cfg.replay_activation_delay_seconds for r in con.execute(
        "SELECT bar_end_utc, MIN(available_at_utc) a FROM bars WHERE source=? AND contract_id=? AND timeframe=?"
        " AND trading_day=? AND session_type='RTH' GROUP BY bar_start_utc ORDER BY bar_start_utc",
        (row["source"], cid, scope.timeframe, day.isoformat()))]
    wm = runner.get_run(con, run_id)["watermark_utc"]
    for t in sorted(set(plan)):
        if t > as_of_utc:
            break
        if t < wm:
            continue
        step = runner.step(settings, con, run_id, strategy, t, risk_cfg=risk_cfg, engine_cfg=engine_cfg)
    if step is None or step.get("status") != "OK":      # 重复调用：取这个运行最后一条扫描记录
        last = con.execute("SELECT decision_json FROM scan_events WHERE run_id=? ORDER BY as_of_utc DESC LIMIT 1",
                           (run_id,)).fetchone()
        step = json.loads(last[0]) if last else None
    return _merge(settings, con, scope, validation, step, run_id, None)


# ---------------------------------------------------------------- 中文短报
def _generic_structure(meta: dict | None) -> str:
    """策略结构的通用展示：只列元数据 structure 里的标量字段，不解释任何策略专有的含义。"""
    if not meta:
        return "策略结构：不可用"
    items = [f"{k}={v}" for k, v in (meta.get("structure") or {}).items()
             if isinstance(v, (str, int, bool)) and v != "" and not k.endswith("_note")]
    return "策略结构：" + ("，".join(items) if items else "无")


def render(settings: Settings, r: dict) -> str:
    tick = settings.instrument(r["scope"].split(":")[1]).tick_size
    px = lambda t: "—" if t is None else str(t * tick)          # noqa: E731
    et = lambda iso_: sessions.et(sessions.parse_utc(iso_)).strftime("%H:%M:%S")     # noqa: E731
    as_of = sessions.et(sessions.parse_utc(r["as_of_utc"])).strftime("%Y-%m-%d %H:%M:%S ET")
    vt = r["version_tuple"]
    ver = f"{vt.get('strategy_family')}@{vt.get('strategy_version') or '—'} / {vt.get('engine_version')}"
    status = r["signal_status"] if r["risk_status"] != "BLOCKED" else f"{r['signal_status']}·BLOCKED"
    tag = " [FIXTURE]" if r["fixture"] else ""
    L = [f"[{as_of}] [{r['scope']}] [{r['contract_id']}] [{status}] [{ver}]{tag}"]
    lb, lv, meta = r["latest_bar"], r["reference_levels"], r["decision_metadata"]
    if lb:
        L.append(f"事实：最新信号 K {et(lb['bar_start_utc'])}–{et(lb['bar_end_utc'])} ET，开 {lb['open']} 高 {lb['high']} "
                 f"低 {lb['low']} 收 {lb['close']}。{_generic_structure(meta)}。"
                 f"PDH {lv['pdh'] or '不可用'} / PDL {lv['pdl'] or '不可用'} / 上一 RTH 收盘 {lv['prev_rth_close'] or '不可用'}，"
                 f"ONH {lv['onh'] or '不可用'} / ONL {lv['onl'] or '不可用'}，ADR 有效日 {r['adr_valid_days']}/10。")
    else:
        L.append("事实：没有可见的 K 线。")
    if r["signal_status"] == "DATA_ERROR":
        det = "；".join(f"{p['category']}（{p.get('detail', '')}）" for p in r["problems"])
        L.append(f"决策：DATA_ERROR，不产生新信号。原因：{det}。下一确认点：数据恢复后重新扫描。")
    elif not r["strategy_ran"]:
        L.append(f"决策：{r['signal_status']}。{r['strategy_note'] or '策略没有运行。'}")
    elif r["signal_status"] == "SETUP_VALID" and r["order_id"]:
        it = r["order_intent"]
        L.append(f"决策：新信号 {meta.get('setup')} {meta.get('label') or ''}，{'做多' if it['side'] == 'LONG' else '做空'}。"
                 f"纸面挂单 {it['order_type']}：触发 {px(it['trigger_ticks'])}，限价 {px(it['limit_ticks'])}，"
                 f"止损 {px(it['stop_ticks'])}，目标 {it['target']['kind']}={it['target']['value']}；"
                 f"有效至 {sessions.et(meta['valid_until_utc']).strftime('%H:%M ET')}。")
    elif r["signal_status"] == "SETUP_VALID":
        L.append(f"决策：有信号 {meta.get('setup')} {meta.get('label') or ''}，但没有挂单。原因：{r['block_reason']}。")
    else:
        why = f"过滤或等待原因：{meta.get('skip_reason')}。" if meta.get("skip_reason") else "没有新信号。"
        L.append(f"决策：WAIT。{why}下一确认点：{meta.get('next_confirmation') or '下一根 K 线收线后'}。")
    pos, wo = r["paper_position"], r["working_order"]
    if pos:
        L.append(f"持仓：纸面{'多' if pos['side'] == 'LONG' else '空'}仓 {pos['quantity']} 手，成交 {px(pos['entry_ticks'])}，"
                 f"止损 {px(pos['stop_ticks'])}，目标 {px(pos['target_ticks'])}。下一触发点：止损或目标先到者，或截止强平。")
    elif wo:
        L.append(f"持仓：无。挂单 {wo['status']}：触发 {px(wo['trigger_ticks'])}，"
                 f"到 {sessions.et(wo['expires_at_utc']).strftime('%H:%M ET')} 过期。")
    else:
        L.append("持仓：无纸面仓位，无挂单。")
    st = (meta or {}).get("structure") or {}
    if "halted" in st:
        L.append(f"纪律：今日纸面止损 {st.get('stops_today', 0)} 次；"
                 + ("已停手，今日不再尝试。" if st["halted"] else "允许在出现新的 setup 时再尝试。"))
    else:
        L.append("纪律：没有可用的策略纪律状态。")
    nw = r["news_windows"]
    if not nw["available"]:
        news = "资讯窗口：不可用（未配置）"
    else:
        f = lambda w: f"{w['label']} {sessions.et(w['start_utc']).strftime('%H:%M')}–{sessions.et(w['end_utc']).strftime('%H:%M')} ET"   # noqa: E731
        news = ("资讯窗口：" + ("正在进行 " + "、".join(f(w) for w in nw["active"]) if nw["active"] else "当前不在窗口内")
                + ("；4 小时内 " + "、".join(f(w) for w in nw["upcoming"]) if nw["upcoming"] else ""))
    L.append(f"数据：快照 {r['snapshot_id']}，来源 {r['source'] or '无'}，"
             + (f"最新 bar 完成 {et(lb['bar_end_utc'])} ET、到达 {et(lb['available_at_utc'])} ET"
                f"{'（估计）' if lb['available_at_estimated'] else ''}" if lb else "没有最新 bar")
             + ("；当日为降级日" if r["degraded_day"] else "") + f"。{news}。")
    L.append(DISCLAIMER)
    return "\n".join(L)
