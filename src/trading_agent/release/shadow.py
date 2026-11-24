"""正式影子（SHADOW）与实时前向预演（PRE_EVAL_SHADOW）。

正式影子只接收 PASS_FOR_SHADOW 的候选。登记时冻结计划（起始日、有效日数、每臂最低成交、检查项、窗口性质、
可选的非劣效设置），之后不可修改；到计划的终点才判定，没有延长窗口的入口。两臂用同一决策时点推进，各写各的账本。
影子是运行验收：检查数据、订单、账本、持仓与冻结；收益只作描述，不替代正式确认。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from .. import sessions
from ..config import Scope, Settings
from ..engine import runner
from ..engine.config import EngineConfig, RiskConfig
from ..evolve import candidates as cands
from ..evolve.queue import ENGINE_FLAGS
from ..research import metrics
from ..scan import _session_for, run_scan
from . import manifest as rman
from . import pointer
from .config import ReleaseConfig
from .guard import ReleaseError

PASSED, REJECTED, INSUFFICIENT = "PASSED", "REJECTED", "EVIDENCE_INSUFFICIENT"
CAND_STATUS = {REJECTED: "REJECTED", INSUFFICIENT: "EVIDENCE_INSUFFICIENT"}
CHECKS = ("DATA_COMPLETE", "LEDGER_CONSISTENT", "FLAT_AT_DAY_END", "NO_ENGINE_FLAGS", "NO_ENTRY_WHILE_FROZEN",
          "MIN_FILLS_PER_ARM")


def sha(obj) -> str:
    return hashlib.sha256(pointer.canon(obj).encode()).hexdigest()


def arm_run_id(shadow_id: str, arm: str) -> str:
    return f"shadow:{shadow_id}:{arm}"


def last_data_day(con: sqlite3.Connection, root: str) -> str | None:
    r = con.execute("SELECT MAX(trading_day) FROM trading_days WHERE instrument_root=?", (root,)).fetchone()
    return r[0] if r else None


def plan_days(cfg: ReleaseConfig, trades_per_day: float | None) -> tuple[int, bool]:
    """有效日数：至少 min_valid_days；预期成交不够最低数时按频次加长，不超过 max_valid_days。"""
    s = cfg.shadow
    need = s["min_valid_days"]
    if trades_per_day and trades_per_day > 0:
        need = max(need, math.ceil(s["min_fills_per_arm"] / (trades_per_day * s["expected_fill_ratio"])))
    days = min(need, s["max_valid_days"])
    expected = (trades_per_day or 0) * days * s["expected_fill_ratio"]
    return days, expected < s["min_fills_per_arm"]


def register(settings: Settings, con: sqlite3.Connection, release_con, scope: Scope, candidate_id: str,
             cfg: ReleaseConfig, *, registry, risk_cfg: RiskConfig, start_day: str | None = None,
             now: int | None = None) -> dict:
    now = now or int(time.time())
    c = cands.get(con, candidate_id)
    if c is None or c["scope"] != str(scope):
        raise ReleaseError("NO_CANDIDATE", f"{scope} 没有候选 {candidate_id}")
    if c["status"] != "PASS_FOR_SHADOW":
        raise ReleaseError("NOT_PASS_FOR_SHADOW", f"候选 {candidate_id} 的状态是 {c['status']}：只有通过正式确认的候选进入影子")
    active = pointer.resolve(release_con, scope)
    if active is None:
        raise ReleaseError("NO_ACTIVE", f"{scope} 还没有 active 版本：基线臂没有可用的版本，先建立首次纸面基线")
    if not rman.base_matches(c["base_release"], active):
        raise ReleaseError("STALE_BASE", f"候选基于 {c['base_release']}，当前 active 是 {active['release_id']}")
    last = last_data_day(con, scope.instrument_root)
    today, _ = _session_for(now)
    if start_day is None:
        ref = max(today, date.fromisoformat(last)) if last else today
        start_day = sessions.next_trading_day(ref).isoformat()
    if (last and start_day <= last) or start_day <= today.isoformat():
        raise ReleaseError("DISCLOSED_DATES", f"影子起始日 {start_day} 不晚于已有数据或今天（最后一个数据日 {last}）：已披露的日期不能用")
    base = rman.frozen_baseline(con, str(scope))
    dev = rman.dev_expectations(settings, con, scope, base, risk_cfg)
    days, low = plan_days(cfg, dev["trades_per_day"])
    base_diff = dict((active.get("manifest") or {}).get("config_diff") or {})
    cand_diff = {**base_diff, **{d["key"]: d["to"] for d in c["candidate"]["exact_diff"]}}
    s = cfg.shadow
    plan = {"start_day": start_day, "valid_days": days, "min_fills_per_arm": s["min_fills_per_arm"],
            "max_data_error_days": s["max_data_error_days"], "window_kind": s["window_kind"],
            "noninferiority": s["noninferiority"], "checks": list(CHECKS),
            "expected_trades_per_day": dev["trades_per_day"], "low_frequency": low,
            "base_config_diff": base_diff, "cand_config_diff": cand_diff,
            "policy_version": cfg.version, "policy_sha256": cfg.digest}
    plan_sha = sha(plan)
    shadow_id = "shd_" + sha([candidate_id, active["release_id"], plan_sha])[:16]
    if con.execute("SELECT 1 FROM shadow_registrations WHERE shadow_id=?", (shadow_id,)).fetchone():
        raise ReleaseError("ALREADY_REGISTERED", f"影子 {shadow_id} 已经登记过")
    for arm, diff in (("base", base_diff), ("cand", cand_diff)):
        strategy = rman.strategy_for(registry, scope, diff, shadow_id[-6:] + arm[0])
        runner.create_run(con, scope, strategy, "live_shadow", arm_run_id(shadow_id, arm))
    with con:
        con.execute("INSERT INTO shadow_registrations VALUES (?,?,?,?,?,?,?,?,?)",
                    (shadow_id, candidate_id, str(scope), active["release_id"], pointer.canon(plan), plan_sha,
                     arm_run_id(shadow_id, "base"), arm_run_id(shadow_id, "cand"), now))
    cands.set_status(con, candidate_id, "SHADOW", f"SHADOW_REGISTERED:{shadow_id}", now=now)
    return {"shadow_id": shadow_id, "candidate_id": candidate_id, "base_release": active["release_id"],
            "plan": plan, "plan_sha256": plan_sha}


def open_registrations(con: sqlite3.Connection, scope_text: str) -> list[dict]:
    out = []
    for r in con.execute("SELECT s.* FROM shadow_registrations s LEFT JOIN shadow_results x ON x.shadow_id=s.shadow_id"
                         " WHERE s.scope=? AND x.shadow_id IS NULL ORDER BY s.created_at_utc", (scope_text,)):
        d = {k: r[k] for k in r.keys()}
        d["plan"] = json.loads(d.pop("plan_json"))
        out.append(d)
    return out


def reserved_window_start(con: sqlite3.Connection, scope_text: str) -> str | None:
    """进行中的前向保留窗口里最早的起始日：从这天起的交易日不进作者的复盘和聚合输入。"""
    try:
        regs = open_registrations(con, scope_text)
    except sqlite3.OperationalError:
        return None
    days = [r["plan"]["start_day"] for r in regs if r["plan"].get("window_kind") == "FORWARD_HOLDOUT"]
    return min(days) if days else None


def _set_block(con, scope: Scope, domain: str, blocked: bool, reason: str | None, as_of: int):
    with con:
        con.execute(
            "INSERT INTO scope_data_status VALUES (?,?,?,?,?,1,?) ON CONFLICT(scope, risk_domain) DO UPDATE SET"
            " blocked=excluded.blocked, reason=excluded.reason, since_utc=excluded.since_utc, version=version+1,"
            " updated_at_utc=excluded.updated_at_utc",
            (str(scope), domain, int(blocked), reason, as_of if blocked else None, int(time.time())))


def _step_run(settings, con, scope, run_id, strategy, now, validation, risk_cfg, engine_cfg) -> dict:
    run = runner.get_run(con, run_id)
    _set_block(con, scope, run["risk_domain"], validation["signal_status"] == "DATA_ERROR",
               validation["block_reason"], now)
    r = runner.step(settings, con, run_id, strategy, max(now, run["watermark_utc"]), risk_cfg=risk_cfg,
                    engine_cfg=engine_cfg)
    return {"run_id": run_id, "status": r.get("status"), "signal_status": r.get("signal_status"),
            "risk_status": r.get("risk_status"), "block_reason": r.get("block_reason"), "as_of_utc": r.get("as_of_utc")}


def step(settings: Settings, con: sqlite3.Connection, scope: Scope, cfg: ReleaseConfig, *, registry,
         risk_cfg: RiskConfig, engine_cfg: EngineConfig, now_utc: int | None = None, pre_eval: bool = True) -> dict:
    """以同一决策时点推进全部进行中的影子两臂，以及队列前几名的实时前向预演。"""
    now = int(time.time()) if now_utc is None else now_utc
    day, _ = _session_for(now)
    validation = run_scan(settings, con, scope, now_utc=now, record_event=False)
    shadows = []
    for reg in open_registrations(con, str(scope)):
        if day.isoformat() < reg["plan"]["start_day"]:
            shadows.append({"shadow_id": reg["shadow_id"], "status": "NOT_STARTED", "start_day": reg["plan"]["start_day"]})
            continue
        arms = {}
        for arm in ("base", "cand"):
            strategy = rman.strategy_for(registry, scope, reg["plan"][f"{arm}_config_diff"], reg["shadow_id"][-6:] + arm[0])
            arms[arm] = _step_run(settings, con, scope, reg[f"{arm}_run_id"], strategy, now, validation, risk_cfg, engine_cfg)
        shadows.append({"shadow_id": reg["shadow_id"], "candidate_id": reg["candidate_id"], "as_of_utc": now, "arms": arms})
    pre = _pre_eval(settings, con, scope, cfg, registry=registry, risk_cfg=risk_cfg, engine_cfg=engine_cfg, now=now,
                    day=day.isoformat(), validation=validation) if pre_eval else []
    return {"as_of_utc": now, "trading_day": day.isoformat(), "data_status": validation["signal_status"],
            "shadows": shadows, "pre_eval_shadow": pre}


def _pre_eval(settings, con, scope, cfg, *, registry, risk_cfg, engine_cfg, now, day, validation) -> list[dict]:
    """队列前几名挂实时行情：只记可运行性和引擎异常，不报告盈亏。看过的日期不得再进它们的确认窗。"""
    from ..evolve.config import load_evolve
    from ..evolve.queue import rank
    from ..research.config import load_research
    try:
        ranked = rank(con, str(scope), load_evolve(settings.project_root), load_research(settings.project_root).power)
    except Exception:  # noqa: BLE001 — 没有队列配置时不做预演
        return []
    out = []
    for x in ranked[:cfg.pre_eval_top_n]:
        cid = x["candidate_id"]
        c = cands.get(con, cid)
        if c is None or c["status"] != "PROPOSED":
            continue
        run_id = f"pre:{cid}"
        error = None
        strategy = None
        try:
            strategy = rman.strategy_for(registry, scope, {d["key"]: d["to"] for d in c["candidate"]["exact_diff"]}, cid[-8:])
            if con.execute("SELECT 1 FROM engine_runs WHERE run_id=?", (run_id,)).fetchone() is None:
                runner.create_run(con, scope, strategy, "pre_eval_shadow", run_id)
            r = _step_run(settings, con, scope, run_id, strategy, now, validation, risk_cfg, engine_cfg)
        except Exception as e:  # noqa: BLE001 — 跑不起来本身就是预演要报告的事
            error, r = f"{type(e).__name__}: {e}", {}
        flags = sum(f in ENGINE_FLAGS for (fj,) in con.execute(
            "SELECT flags_json FROM paper_trades WHERE run_id=? AND trading_day=?", (run_id, day)) for f in json.loads(fj))
        res = {"runnable": error is None, "error": error, "engine_flags": flags,
               "data_error": validation["signal_status"] == "DATA_ERROR"}
        with con:
            con.execute("INSERT INTO pre_eval_shadow_days VALUES (?,?,?,?) ON CONFLICT(candidate_id, trading_day)"
                        " DO UPDATE SET result_json=excluded.result_json", (cid, day, pointer.canon(res), now))
        out.append({"candidate_id": cid, "trading_day": day, **res, "step_status": r.get("status")})
    return out


# ---------------------------------------------------------------- 终点判定
def _window(con, scope: Scope, reg: dict) -> dict:
    plan = reg["plan"]
    rows = con.execute("SELECT trading_day, complete, degraded FROM trading_days WHERE instrument_root=? AND trading_day>=?"
                       " ORDER BY trading_day", (scope.instrument_root, plan["start_day"])).fetchall()

    def seen(run_id):
        return {r[0] for r in con.execute("SELECT DISTINCT json_extract(decision_json,'$.trading_day') FROM scan_events"
                                          " WHERE run_id=?", (run_id,))}
    both = seen(reg["base_run_id"]) & seen(reg["cand_run_id"])
    valid, invalid = [], []
    for d, complete, degraded in rows:
        if len(valid) >= plan["valid_days"]:
            break
        if not complete or degraded:
            invalid.append(d)
        elif d in both:
            valid.append(d)
    reached = len(valid) >= plan["valid_days"] or len(invalid) > plan["max_data_error_days"]
    return {"valid_days": valid, "invalid_days": invalid, "reached": reached}


def entries_while_frozen(con: sqlite3.Connection, scope: Scope, run_id: str) -> list[str]:
    """挂单时（墙钟）该风险域处于冻结的订单。"""
    run = runner.get_run(con, run_id)
    ev = con.execute("SELECT cause, action, created_at_utc FROM freeze_events WHERE scope=? AND risk_domain IN (?, '*')"
                     " ORDER BY created_at_utc, rowid", (str(scope), run["risk_domain"])).fetchall()
    bad = []
    for oid, created in con.execute("SELECT order_id, created_at_utc FROM paper_orders WHERE run_id=?", (run_id,)):
        last: dict = {}
        for cause, action, t in ev:
            if t < created:
                last[cause] = action
        if any(a == "FREEZE" for a in last.values()):
            bad.append(oid)
    return bad


def _arm_summary(settings, con, scope, run_id, days, risk_cfg) -> dict:
    trades = [t for t in metrics.load_trades(settings, con, scope, run_id, risk_cfg) if t["trading_day"] in set(days)]
    ser = metrics.daily_series(trades, days, risk_cfg.research_risk_unit_usd)
    setups = con.execute("SELECT COUNT(*) FROM scan_events WHERE run_id=? AND signal_status='SETUP_VALID'"
                         " AND json_extract(decision_json,'$.trading_day') IN (%s)" % ",".join("?" * len(days)),
                         (run_id, *days)).fetchone()[0] if days else 0
    net = ser["net"]
    return {"trades": len(trades), "trades_per_day": len(trades) / len(days) if days else None,
            "setups_per_day": setups / len(days) if days else None, "unit": ser["unit"],
            "net_per_day": sum(net) / len(net) if net else None, "max_drawdown": metrics.max_drawdown(net) if net else None,
            "daily": net}


def finish(settings: Settings, con: sqlite3.Connection, scope: Scope, candidate_id: str, cfg: ReleaseConfig, *,
           risk_cfg: RiskConfig, now: int | None = None, out_dir: Path | None = None) -> dict:
    now = now or int(time.time())
    reg, res = rman.latest_shadow(con, candidate_id)
    if reg is None:
        raise ReleaseError("NO_SHADOW", f"候选 {candidate_id} 没有登记影子")
    if res is not None:
        return {"status": "ALREADY_FINISHED", "shadow_id": reg["shadow_id"], "result": res["status"]}
    plan = reg["plan"]
    w = _window(con, scope, reg)
    if not w["reached"]:
        raise ReleaseError("NOT_AT_ENDPOINT", f"影子 {reg['shadow_id']} 只走完 {len(w['valid_days'])}／{plan['valid_days']}"
                                              " 个有效日：不到预登记终点不能判定，也不能延长或提前")
    days = w["valid_days"]
    end = days[-1] if days else plan["start_day"]
    after_end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    checks, reasons = {}, []
    checks["DATA_COMPLETE"] = len(w["invalid_days"]) <= plan["max_data_error_days"]
    arms = {}
    for arm, key in (("baseline", "base_run_id"), ("candidate", "cand_run_id")):
        run_id = reg[key]
        led = rman.ledger_check(con, run_id, through_day=after_end)
        probs = led["problems"]
        checks[f"LEDGER_CONSISTENT:{arm}"] = not any(p.startswith(("FILLED_WITHOUT", "CLOSED_WITHOUT")) for p in probs)
        checks[f"FLAT_AT_DAY_END:{arm}"] = not any(p.startswith("OPEN_AFTER_DAY") for p in probs)
        checks[f"NO_ENGINE_FLAGS:{arm}"] = not any(p.startswith("ENGINE_FLAG") for p in probs)
        frozen = entries_while_frozen(con, scope, run_id)
        checks[f"NO_ENTRY_WHILE_FROZEN:{arm}"] = not frozen
        arms[arm] = {**_arm_summary(settings, con, scope, run_id, days, risk_cfg), "ledger_problems": probs[:10],
                     "entries_while_frozen": frozen[:10]}
    fills_ok = all(a["trades"] >= plan["min_fills_per_arm"] for a in arms.values())
    checks["MIN_FILLS_PER_ARM"] = fills_ok
    status = PASSED
    failed_ops = [k for k, v in checks.items() if not v and k not in ("DATA_COMPLETE", "MIN_FILLS_PER_ARM")]
    if failed_ops:
        status, reasons = REJECTED, failed_ops
    elif not checks["DATA_COMPLETE"]:
        status, reasons = INSUFFICIENT, ["DATA_ERROR_DAYS"]
    elif not fills_ok:
        status, reasons = INSUFFICIENT, ["TOO_FEW_FILLS"]
    ni = None
    if status == PASSED and plan["noninferiority"]["enabled"]:
        from ..evaluation import stats
        from ..evaluation.config import load_eval
        ec = load_eval(settings.project_root)
        p = plan["noninferiority"]
        diffs = [c - b for b, c in zip(arms["baseline"]["daily"], arms["candidate"]["daily"])]
        bound = stats.block_bound(diffs, alpha=p["alpha"], seed=ec.seed, block=ec.block_length_days,
                                  resamples=ec.resamples, min_tail=ec.min_tail_count, max_resamples=ec.max_resamples)
        dd_inc = (arms["candidate"]["max_drawdown"] or 0) - (arms["baseline"]["max_drawdown"] or 0)
        ni = {"bound": bound["bound"], "mean": bound["mean"], "margin": p["margin_r_per_day"], "reason": bound["reason"],
              "drawdown_increase": dd_inc}
        if bound["bound"] is None:
            status, reasons = INSUFFICIENT, ["NONINFERIORITY_UNESTIMABLE"]
        elif bound["bound"] <= -p["margin_r_per_day"] or dd_inc > p["max_drawdown_increase_r"]:
            status, reasons = REJECTED, ["NONINFERIORITY_FAILED"]
    for a in arms.values():
        a.pop("daily")
    result = {"shadow_id": reg["shadow_id"], "candidate_id": candidate_id, "status": status, "reasons": reasons,
              "valid_days": len(days), "first_day": days[0] if days else None, "last_day": end,
              "invalid_days": w["invalid_days"], "checks": checks, "arms": arms, "noninferiority": ni,
              "plan_sha256": reg["plan_sha256"],
              "note": "影子是运行验收：收益只作描述，不替代正式确认"}
    with con:
        con.execute("INSERT INTO shadow_results VALUES (?,?,?,?,?)",
                    (reg["shadow_id"], status, pointer.canon(result), sha(result), now))
    if status in CAND_STATUS:
        cands.set_status(con, candidate_id, CAND_STATUS[status], f"SHADOW:{status}", now=now)
    else:
        with con:
            cands.ledger(con, candidate_id, str(scope), None, "SHADOW_PASSED", now=now, detail={"shadow_id": reg["shadow_id"]})
    result["report"] = _write_report(settings, result, out_dir)
    return result


def _write_report(settings: Settings, r: dict, out_dir: Path | None) -> str:
    d = Path(out_dir) if out_dir else settings.project_root / "reports" / "shadow"
    d.mkdir(parents=True, exist_ok=True)
    L = [f"# 影子结果 {r['shadow_id']}", "", f"- 候选：`{r['candidate_id']}`",
         f"- 结论：**`{r['status']}`**" + (f"（{'、'.join(r['reasons'])}）" if r["reasons"] else ""),
         f"- 有效日：{r['valid_days']}（{r['first_day']} 至 {r['last_day']}）；数据异常日：{len(r['invalid_days'])}", "",
         "| 检查 | 通过 |", "| --- | --- |"] + [f"| {k} | {'是' if v else '否'} |" for k, v in r["checks"].items()]
    L += ["", "| 臂 | 成交 | 每日成交 | 每日信号 | 日均净值 | 最大回撤 |", "| --- | --- | --- | --- | --- | --- |"]
    for k, a in r["arms"].items():
        L.append(f"| {k} | {a['trades']} | {a['trades_per_day']} | {a['setups_per_day']} | {a['net_per_day']} "
                 f"| {a['max_drawdown']} |")
    L += ["", r["note"] + "。这是纸面模拟，不是投资建议。"]
    p = d / f"{r['shadow_id']}.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return str(p)
