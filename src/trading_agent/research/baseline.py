"""基线冻结：清单、回放编排、两次运行的一致性校验。

回放只经由 engine.runner.replay。这里不产生信号、不做风控判定、不模拟成交。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import subprocess
import time
from datetime import date
from pathlib import Path

from .. import sessions
from ..config import Scope, Settings
from ..engine import runner
from ..engine.config import EngineConfig, RiskConfig, load_engine, load_risk
from ..engine.fills import ENGINE_VERSION
from . import splits
from .config import Perturbation, ResearchConfig, load_research

FROZEN, PENDING, NON_DETERMINISTIC = "FROZEN", "PENDING", "NON_DETERMINISTIC"
# 决定回放结果的代码：策略、引擎、特征、行情背景和时段。报告、命令行和人工轨不在其中。
CODE_PATHS = ("strategies", "engine", "features.py", "context.py", "sessions.py", "contracts.py", "bars.py",
              "derive.py", "days.py", "config.py")
# 随运行变化、与结果无关的标识：算摘要时去掉
_VOLATILE = {"run_id", "event_id", "order_id", "position_id", "trade_id", "fill_id", "created_at_utc",
             "updated_at_utc"}
_LEDGER = ("paper_fills", "paper_trades", "paper_positions", "paper_orders")


class BaselineError(RuntimeError):
    pass


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _sha(obj) -> str:
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in _VOLATILE}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


# ---------------------------------------------------------------- 清单
def describe_strategy(strategy) -> dict:
    """策略的可复现描述。只用通用约定：config 若是 dataclass 就整体入清单。"""
    cfg = getattr(strategy, "config", None)
    conf = json.loads(_canon(dataclasses.asdict(cfg))) if dataclasses.is_dataclass(cfg) else {}
    req = strategy.feature_requirements()
    setups = conf.get("setups") if isinstance(conf.get("setups"), list) else []
    return {"strategy_family": strategy.strategy_family, "strategy_version": strategy.version,
            "config": conf, "config_sha256": _sha(conf),
            "enabled_setups": [s["name"] for s in setups if isinstance(s, dict) and s.get("enabled")],
            "features": sorted(req.features), "warmup_bars": req.warmup_bars}


def indicator_definitions() -> dict:
    from .. import features
    return {k: getattr(features, k) for k in dir(features) if k.isupper() and isinstance(getattr(features, k), int)}


def code_digest(project_root: Path) -> str:
    base = Path(project_root) / "src" / "trading_agent"
    h = hashlib.sha256()
    for rel in CODE_PATHS:
        p = base / rel
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            h.update(str(f.relative_to(base)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def git_info(project_root: Path) -> dict:
    def run(*args):
        try:
            r = subprocess.run(["git", *args], cwd=project_root, capture_output=True, text=True, timeout=20)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    status = run("status", "--porcelain")
    return {"commit": run("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


def market_manifest(con: sqlite3.Connection, scope: Scope, days: list[str]) -> dict:
    """回放区间的行情清单：逐日的来源、实际合约和所有 revision 的内容。任何一根出现新 revision 都会改变摘要。"""
    h = hashlib.sha256()
    bars = signal = estimated = 0
    per_source: dict[str, int] = {}
    for d in days:
        row = con.execute("SELECT source, contract_id, complete, degraded FROM trading_days WHERE instrument_root=?"
                          " AND trading_day=?", (scope.instrument_root, d)).fetchone()
        if row is None or row["source"] is None:
            h.update(f"{d}|NO_DATA\n".encode())
            continue
        h.update(f"{d}|{row['source']}|{row['contract_id']}|{row['complete']}|{row['degraded']}\n".encode())
        per_source[row["source"]] = per_source.get(row["source"], 0) + 1
        for r in con.execute(
                "SELECT timeframe, bar_start_utc, revision, open, high, low, close, volume, available_at_utc,"
                " available_at_estimated, derived, session_type FROM bars WHERE source=? AND contract_id=?"
                " AND trading_day=? ORDER BY timeframe, bar_start_utc, revision",
                (row["source"], row["contract_id"], d)):
            h.update(("|".join(str(x) for x in tuple(r)[:11]) + "\n").encode())
            bars += 1
            if r["timeframe"] == scope.timeframe and r["session_type"] == scope.session_type:
                signal += 1
                estimated += r["available_at_estimated"]
    return {"days": len(days), "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
            "bars": bars, "sha256": h.hexdigest(), "days_by_source": per_source, "signal_bars": signal,
            "signal_bars_estimated_available_at": estimated}


def build_manifest(settings: Settings, con: sqlite3.Connection, scope: Scope, strategy, reg: dict,
                   research_cfg: ResearchConfig, risk_cfg: RiskConfig, engine_cfg: EngineConfig) -> dict:
    days = sorted(splits.segment_days(reg, splits.DEVELOPMENT, valid_only=False)
                  + splits.segment_days(reg, splits.FORWARD, valid_only=False))
    if not days:
        raise BaselineError("开发可见区间内没有交易日")
    inst = settings.instrument(scope.instrument_root)
    return {
        "scope": str(scope), "strategy": describe_strategy(strategy), "indicators": indicator_definitions(),
        "engine_version": ENGINE_VERSION,
        "engine": {"flatten_minutes_before_close": engine_cfg.flatten_minutes_before_close,
                   "replay_activation_delay_seconds": engine_cfg.replay_activation_delay_seconds},
        "risk": {"risk_version": risk_cfg.version, "fee_mode": risk_cfg.fee_mode,
                 "fee_amount_usd": None if risk_cfg.fee_amount_usd is None else str(risk_cfg.fee_amount_usd),
                 "entry_slippage_ticks": risk_cfg.entry_slippage_ticks, "stop_slippage_ticks": risk_cfg.stop_slippage_ticks,
                 "paper_quantity": risk_cfg.paper_quantity,
                 "research_risk_unit_usd": None if risk_cfg.research_risk_unit_usd is None
                 else str(risk_cfg.research_risk_unit_usd)},
        "session": {"session_type": scope.session_type, "timeframe": scope.timeframe,
                    "calendar_version": sessions.CALENDAR_VERSION, "roll_rule_version": inst.roll_rule_version,
                    "tick_size": str(inst.tick_size), "tick_value_usd": str(inst.tick_value_usd)},
        "data": {**market_manifest(con, scope, days), "available_at_delay_seconds": settings.available_at_delay_seconds,
                 "source_priority": list(settings.source_priority),
                 "dev_visible_through": settings.dev_visible_through.isoformat() if settings.dev_visible_through else None},
        "splits": {"registry_id": reg["registry_id"], "content_sha256": reg["content_sha256"]},
        "prompt_digest": None,                        # 回放不调用模型，没有提示词参与
        "random_seed": None,                          # 回放是确定性的，没有随机数
        "carry_over": "持仓不跨交易日：截止强平在 RTH 收盘前平掉全部仓位并撤掉挂单，所以不会跨切分窗口",
        "warmup": {"rule": "指标预热只用回放起点之前已可见的 K 线",
                   "excluded_trading_days": research_cfg.warmup_trading_days},
        "code_sha256": code_digest(settings.project_root),
        "fixture": reg["fixture"],
    }


def baseline_id_of(manifest: dict) -> str:
    return "bl_" + _sha(manifest)[:16]


# ---------------------------------------------------------------- 运行摘要
def run_digests(con: sqlite3.Connection, run_id: str) -> dict:
    """一次运行的逐笔、逐张订单、逐条决策的内容摘要。不含随运行变化的标识和写入时间。"""
    trades = [tuple(r) for r in con.execute(
        "SELECT trading_day, contract_id, setup_id, side, quantity, entry_ticks, entry_time_utc, exit_ticks,"
        " exit_time_utc, exit_reason, initial_risk_ticks, pnl_ticks, gross_usd, fees_usd, net_usd, net_sign,"
        " r_initial, r_research, flags_json, version_tuple_json FROM paper_trades WHERE run_id=?"
        " ORDER BY entry_time_utc, exit_time_utc, setup_id", (run_id,))]
    orders = [tuple(r) for r in con.execute(
        "SELECT setup_id, contract_id, side, order_type, status, signal_bar_end_utc, armed_at_utc, expires_at_utc,"
        " trigger_ticks, limit_ticks, stop_ticks, target_json, quantity, bars_seen, flags_json, end_reason, intent_json"
        " FROM paper_orders WHERE run_id=? ORDER BY signal_bar_end_utc, setup_id", (run_id,))]
    h = hashlib.sha256()
    n = 0
    for r in con.execute("SELECT as_of_utc, signal_status, risk_status, paper_action, strategy_state_json,"
                         " decision_json FROM scan_events WHERE run_id=? ORDER BY as_of_utc, event_id", (run_id,)):
        h.update(_canon([r[0], r[1], r[2], r[3], r[4], _strip(json.loads(r[5]))]).encode())
        h.update(b"\n")
        n += 1
    return {"trades": _sha(trades), "orders": _sha(orders), "decisions": h.hexdigest(),
            "counts": {"trades": len(trades), "orders": len(orders), "decisions": n}}


def purge_run(con: sqlite3.Connection, run_id: str, *, keep_ledger: bool = False):
    """删除一次运行的明细。keep_ledger 时只删逐条决策，保留订单、仓位和交易。"""
    with con:
        con.execute("DELETE FROM scan_events WHERE run_id=?", (run_id,))
        if not keep_ledger:
            for t in _LEDGER:
                con.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
            con.execute("DELETE FROM engine_runs WHERE run_id=?", (run_id,))


def event_summary(con: sqlite3.Connection, run_id: str, only_days: set[str] | None = None) -> dict:
    """逐条决策的计数：机会数、等待或被拒原因、各交易日所用的子 K 周期。only_days 给定时只数这些交易日。"""
    out = {"decisions": 0, "setups": 0, "skip_reasons": {}, "block_reasons": {}, "sub_bar_days": {}}
    days: dict[str, str] = {}
    for r in con.execute("SELECT signal_status, decision_json FROM scan_events WHERE run_id=?", (run_id,)):
        d = json.loads(r[1])
        if only_days is not None and d.get("trading_day") not in only_days:
            continue
        out["decisions"] += 1
        out["setups"] += r[0] == "SETUP_VALID"
        skip = (d.get("decision_metadata") or {}).get("skip_reason")
        if skip:
            out["skip_reasons"][skip] = out["skip_reasons"].get(skip, 0) + 1
        if d.get("block_reason"):
            out["block_reasons"][d["block_reason"]] = out["block_reasons"].get(d["block_reason"], 0) + 1
        if d.get("trading_day") and d.get("sub_bar_timeframe"):
            days[d["trading_day"]] = d["sub_bar_timeframe"]
    for tf in days.values():
        out["sub_bar_days"][tf] = out["sub_bar_days"].get(tf, 0) + 1
    return out


def _run(settings, con, scope, strategy, baseline_id, run_id, role, start: str, end: str, *, risk_cfg, engine_cfg,
         allow_real_data, variant: Perturbation | None = None, keep: str = "all") -> dict:
    """跑一次回放并登记。keep：all 保留全部明细；ledger 只留账本；none 全删（只留摘要）。"""
    row = con.execute("SELECT status, digests_json, summary_json FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is not None and row["status"] == "DONE":
        return {"run_id": run_id, "digests": json.loads(row["digests_json"]), "summary": json.loads(row["summary_json"]),
                "reused": True}
    purge_run(con, run_id)                            # 上次中断留下的半截运行：清掉重跑
    now = int(time.time())
    with con:
        con.execute("DELETE FROM research_runs WHERE run_id=?", (run_id,))
        con.execute(
            "INSERT INTO research_runs (run_id, baseline_id, scope, role, variant_id, track, diff_json, start_day,"
            " end_day, status, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,'RUNNING',?,?)",
            (run_id, baseline_id, str(scope), role, variant.id if variant else None, variant.track if variant else None,
             _canon(dict(variant.diff)) if variant else "{}", start, end, now, now))
    t0 = time.time()
    res = runner.replay(settings, con, scope, strategy, date.fromisoformat(start), date.fromisoformat(end),
                        run_id=run_id, risk_cfg=risk_cfg, engine_cfg=engine_cfg, allow_real_data=allow_real_data)
    digests = run_digests(con, run_id)
    summary = {**{k: res[k] for k in ("run_mode", "days", "steps", "setups", "orders", "trades", "pnl_ticks")},
               "strategy_version": strategy.version, "seconds": round(time.time() - t0, 1),
               "events": event_summary(con, run_id)}
    if keep != "all":
        purge_run(con, run_id, keep_ledger=keep == "ledger")
    with con:
        con.execute("UPDATE research_runs SET status='DONE', digests_json=?, summary_json=?, updated_at_utc=?"
                    " WHERE run_id=?", (_canon(digests), _canon(summary), int(time.time()), run_id))
    return {"run_id": run_id, "digests": digests, "summary": summary, "reused": False}


def get(con: sqlite3.Connection, baseline_id: str) -> dict | None:
    r = con.execute("SELECT * FROM baselines WHERE baseline_id=?", (baseline_id,)).fetchone()
    if r is None:
        return None
    return {"baseline_id": r["baseline_id"], "scope": r["scope"], "status": r["status"],
            "manifest": json.loads(r["manifest_json"]), "registry_id": r["registry_id"], "run_id": r["run_id"],
            "digests": json.loads(r["digests_json"]), "flags": json.loads(r["flags_json"])}


def freeze(settings: Settings, con: sqlite3.Connection, scope: Scope, strategy, research_cfg: ResearchConfig | None = None,
           *, risk_cfg: RiskConfig | None = None, engine_cfg: EngineConfig | None = None,
           allow_real_data: bool = False, log=lambda msg: None) -> dict:
    """冻结一个 scope 的基线：登记切分 → 清单 → 回放两次 → 比对摘要。已冻结的同一清单直接复用。"""
    research_cfg = research_cfg or load_research(settings.project_root)
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    reg = splits.register(settings, con, scope.instrument_root, research_cfg)
    manifest = build_manifest(settings, con, scope, strategy, reg, research_cfg, risk_cfg, engine_cfg)
    bid = baseline_id_of(manifest)
    start, end = manifest["data"]["first_day"], manifest["data"]["last_day"]
    git = git_info(settings.project_root)
    flags = (["FIXTURE_ONLY"] if manifest["fixture"] else []) + (["DIRTY_WORKTREE"] if git["dirty"] else [])
    for name, missing in (("FEES_NOT_CONFIGURED", not risk_cfg.fees_configured),
                          ("RESEARCH_RISK_UNIT_NOT_SET", risk_cfg.research_risk_unit_usd is None)):
        if missing:
            flags.append(name)
    existing = get(con, bid)
    now = int(time.time())
    if existing is None:
        with con:
            con.execute(
                "INSERT INTO baselines (baseline_id, scope, strategy_family, status, manifest_json, manifest_sha256,"
                " registry_id, flags_json, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (bid, str(scope), scope.strategy_family, PENDING,
                 _canon({**manifest, "git": git}), _sha(manifest), reg["registry_id"], _canon(flags), now, now))
    elif existing["status"] == FROZEN:
        log(f"基线 {bid} 已冻结，直接复用")
        return {**existing, "registry": reg, "reused": True}

    base_id, verify_id = f"base:{bid}", f"verify:{bid}"
    kw = dict(risk_cfg=risk_cfg, engine_cfg=engine_cfg, allow_real_data=allow_real_data)
    log(f"基线 {bid}：第一次回放 {start} 至 {end}")
    a = _run(settings, con, scope, strategy, bid, base_id, "BASE", start, end, **kw)
    log(f"第一次回放完成（{a['summary']['seconds']} 秒，{a['digests']['counts']['trades']} 笔交易）。第二次回放校验")
    b = _run(settings, con, scope, strategy, bid, verify_id, "VERIFY", start, end, keep="none", **kw)
    same = all(a["digests"][k] == b["digests"][k] for k in ("trades", "orders", "decisions"))
    status = FROZEN if same else NON_DETERMINISTIC
    digests = {"base": a["digests"], "verify": b["digests"], "identical": same}
    with con:
        con.execute("UPDATE baselines SET status=?, run_id=?, digests_json=?, version=version+1, updated_at_utc=?"
                    " WHERE baseline_id=?", (status, base_id, _canon(digests), int(time.time()), bid))
    log("两次回放逐笔、逐张订单、逐条决策完全一致" if same else "两次回放不一致：基线不可用")
    return {**get(con, bid), "registry": reg, "reused": False}


def variant_run_id(baseline_id: str, variant_id: str) -> str:
    return f"var:{baseline_id}:{variant_id}"


def run_variant(settings: Settings, con: sqlite3.Connection, scope: Scope, registry, baseline: dict, reg: dict,
                pert: Perturbation, *, risk_cfg: RiskConfig | None = None, engine_cfg: EngineConfig | None = None,
                allow_real_data: bool = False) -> dict:
    """在开发段上回放一个预登记的扰动。保留它的交易和订单，逐条决策只留计数。"""
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    days = splits.segment_days(reg, splits.DEVELOPMENT, valid_only=False)
    if not days:
        raise BaselineError("没有开发段，无法运行扰动")
    strategy = registry.variant(scope.strategy_family, pert.diff, pert.id)
    return _run(settings, con, scope, strategy, baseline["baseline_id"], variant_run_id(baseline["baseline_id"], pert.id),
                "VARIANT", days[0], days[-1], risk_cfg=risk_cfg, engine_cfg=engine_cfg,
                allow_real_data=allow_real_data, variant=pert, keep="ledger")
