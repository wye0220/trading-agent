"""评测流程：预登记 → 盲筛 → 正式确认；标注类评测；回归。

评测在评测方自己的数据库里回放两臂（位于 holdout_root 下）。运行库只收到协议允许的摘要。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from .. import db, sessions
from ..config import Scope, Settings
from ..engine.config import load_engine, load_risk
from ..evolve import candidates as cands
from ..evolve.queue import ENGINE_FLAGS, base_release_of
from ..research import baseline as bl
from ..research import metrics, splits
from ..research.config import Perturbation, ResearchConfig, load_research
from ..research.report import paths as report_paths
from . import gates, guard, stats, windows
from .config import EvalConfig, load_eval

SCREEN, CONFIRM, LABELING = "SCREEN", "CONFIRM", "LABELING"
CAND_STATUS = {gates.CONTINUE: "SCREENED", gates.REJECTED: "FAILED", gates.FAIL: "FAILED",
               gates.PASS: "PASS_FOR_SHADOW", gates.INSUFFICIENT: "RESEARCH_OBSERVATION"}


class EvalError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj) -> str:
    return hashlib.sha256((obj if isinstance(obj, str) else canon(obj)).encode()).hexdigest()


def candidate_digest(c: dict) -> str:
    """候选里被评测的实质内容。证据刷新和状态变化不算改动；差异、轨道、基准变了才算。"""
    body = c["candidate"]
    return sha({k: body.get(k) for k in ("scope", "category", "eval_track", "exact_diff", "base_release", "primary_metric")})


def frozen_baseline(con: sqlite3.Connection, scope_text: str) -> dict:
    r = con.execute("SELECT baseline_id FROM baselines WHERE scope=? AND status='FROZEN' AND run_id IS NOT NULL"
                    " ORDER BY updated_at_utc DESC LIMIT 1", (scope_text,)).fetchone()
    if r is None:
        raise EvalError("NO_BASELINE", f"{scope_text} 没有已冻结的基线")
    return bl.get(con, r[0])


def power_eligibility(settings: Settings, scope: Scope, baseline_id: str, reports_dir: Path | None = None) -> dict:
    """阶段 5 功效报告里各轨道的短窗正式资格。报告不在或不是这条基线的，记 UNKNOWN。"""
    try:
        p = json.loads(report_paths(settings, scope, reports_dir)["power_json"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if p.get("baseline_id") != baseline_id:
        return {}
    return {k: v.get("eligibility") for k, v in (p.get("tracks") or {}).items()}


# ---------------------------------------------------------------- 预登记
def register(settings: Settings, con: sqlite3.Connection, scope: Scope, candidate_ids: list[str], *,
             cfg: EvalConfig | None = None, research_cfg: ResearchConfig | None = None,
             eligibility: dict | None = None, reports_dir: Path | None = None, now: int | None = None) -> dict:
    """解封之前一次登记：候选摘要、基线、协议、两段窗口。同一次登记的候选共用窗口。"""
    cfg = cfg or load_eval(settings.project_root)
    research_cfg = research_cfg or load_research(settings.project_root)
    now = int(time.time()) if now is None else now
    if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
        raise EvalError("NO_CANDIDATE", "需要至少一个候选，且不能重复")
    base = frozen_baseline(con, str(scope))
    man = base["manifest"]
    if man["risk"].get("research_risk_unit_usd") is None:
        raise EvalError("RESEARCH_UNIT_NOT_SET", "基线没有预登记研究风险单位，无法按 R 评测")
    items = []
    for cid in candidate_ids:
        c = cands.get(con, cid)
        if c is None or c["category"] != "strategy_rule" or not c["candidate"]:
            raise EvalError("NO_CANDIDATE", f"没有这个策略类候选：{cid}")
        if c["scope"] != str(scope):
            raise EvalError("SCOPE_MISMATCH", f"候选 {cid} 属于 {c['scope']}")
        if c["status"] != "PROPOSED":
            raise EvalError("NOT_PROPOSED", f"候选 {cid} 的状态是 {c['status']}，只有 PROPOSED 可以登记评测")
        if c["base_release"] != base_release_of(base["baseline_id"]):
            raise EvalError("BASE_REPLACED", f"候选 {cid} 的基准不是当前基线，先对着新基线重新提出")
        if con.execute("SELECT 1 FROM evaluation_registrations WHERE candidate_id=?", (cid,)).fetchone():
            raise EvalError("ALREADY_REGISTERED", f"候选 {cid} 已经登记过评测；改动后的候选是新的候选，要重新提出")
        items.append(c)
    elig = eligibility if eligibility is not None else power_eligibility(settings, scope, base["baseline_id"], reports_dir)
    root = scope.instrument_root
    reg_def = con.execute("SELECT definition_json FROM split_registries WHERE registry_id=?", (base["registry_id"],)).fetchone()
    usage = json.loads(reg_def[0])["default_usage"]
    try:
        alloc = windows.allocate(con, root, base["registry_id"], usage["blind_screen_capsules"], usage["confirm_capsules"])
    except windows.WindowError as e:
        raise EvalError(e.reason, str(e)) from None
    sw, cw = windows.window_of(alloc["screen"]), windows.window_of(alloc["confirm"])
    p = research_cfg.power
    protocol = {
        "evaluation": cfg.body(), "baseline_id": base["baseline_id"], "registry_id": base["registry_id"],
        "scope": str(scope), "engine_sha256": man["code_sha256"], "engine_version": man["engine_version"],
        "strategy_config_sha256": man["strategy"]["config_sha256"], "costs": man["risk"],
        "primary_metric": "paired_net_r_per_day", "one_sided": True,
        "alpha_family": p.alpha_family, "max_formal_tests": p.max_formal_tests, "alpha_per_test": p.alpha,
        "thresholds": {"min_trades_per_arm": p.min_trades_per_arm, "confirm_required_days": len(cw["valid_days"]),
                       "screen_required_days": len(sw["valid_days"]),
                       "stress_extra_ticks_per_side": p.stress_extra_ticks_per_side,
                       "track_a": {"margin_r_per_day": p.margin_r_per_day,
                                   "drawdown_improvement_min_pct": p.drawdown_improvement_min_pct},
                       "track_b": {"delta_r_per_day": p.delta_r_per_day,
                                   "drawdown_increase_max_r": p.drawdown_increase_max_r}},
        "power_eligibility": {k: elig.get(k, "UNKNOWN") for k in (gates.TRACK_A, gates.TRACK_B)},
        "valid_day_rule": "当日实际合约 RTH 完整且来源质量未降级；零交易日计入；不按收益删除日期",
        "missing_data_rule": "窗口内容摘要与登记不符，或有效日不足，报 EVIDENCE_INSUFFICIENT",
        "windows": {"screen": {k: sw[k] for k in ("capsules", "start_day", "end_day", "content")},
                    "confirm": {k: cw[k] for k in ("capsules", "start_day", "end_day", "content")}},
    }
    pid = "proto_" + sha(protocol)[:16]
    batch = "batch_" + sha([pid, sorted(candidate_ids)])[:16]
    out = []
    with con:
        con.execute("INSERT OR IGNORE INTO evaluation_protocols VALUES (?,?,?)", (pid, canon(protocol), now))
        for c in items:
            digest = candidate_digest(c)
            rid = "reg_" + sha([c["candidate_id"], digest, pid])[:16]
            fam = cands.hypothesis_family(c["candidate"])
            con.execute("INSERT INTO evaluation_registrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, batch, c["candidate_id"], digest, str(scope), root, base["baseline_id"], base["registry_id"],
                         pid, fam, c["candidate"]["eval_track"], canon(sw), canon(cw), now))
            windows.record(con, base["registry_id"], root, alloc["screen"], SCREEN, "ASSIGNED", rid, c["candidate_id"], now)
            windows.record(con, base["registry_id"], root, alloc["confirm"], CONFIRM, "ASSIGNED", rid, c["candidate_id"], now)
            cands.ledger(con, c["candidate_id"], str(scope), fam, "EVAL_REGISTERED", now=now,
                         detail={"registration_id": rid, "protocol_id": pid})
            out.append({"registration_id": rid, "candidate_id": c["candidate_id"], "eval_track": c["candidate"]["eval_track"],
                        "power_eligibility": protocol["power_eligibility"].get(c["candidate"]["eval_track"])})
    return {"protocol_id": pid, "batch_id": batch, "baseline_id": base["baseline_id"], "registrations": out,
            "screen_window": {k: sw[k] for k in ("capsules", "start_day", "end_day")} | {"valid_days": len(sw["valid_days"])},
            "confirm_window": {k: cw[k] for k in ("capsules", "start_day", "end_day")} | {"valid_days": len(cw["valid_days"])}}


def registration(con: sqlite3.Connection, candidate_id: str) -> dict:
    r = con.execute("SELECT * FROM evaluation_registrations WHERE candidate_id=? ORDER BY created_at_utc DESC LIMIT 1",
                    (candidate_id,)).fetchone()
    if r is None:
        raise EvalError("NOT_REGISTERED", f"候选 {candidate_id} 没有登记过评测")
    proto = json.loads(con.execute("SELECT body_json FROM evaluation_protocols WHERE protocol_id=?",
                                   (r["protocol_id"],)).fetchone()[0])
    return {**{k: r[k] for k in r.keys() if not k.endswith("_json")}, "screen": json.loads(r["screen_json"]),
            "confirm": json.loads(r["confirm_json"]), "protocol": proto}


def _eval_id(reg: dict, stage: str) -> str:
    return "eval_" + sha([reg["registration_id"], stage])[:16]


def stage_row(con: sqlite3.Connection, reg: dict, stage: str):
    return con.execute("SELECT status, result_json FROM evaluation_runs WHERE evaluation_id=?",
                       (_eval_id(reg, stage),)).fetchone()


# ---------------------------------------------------------------- 回归
def synthetic_regression(settings: Settings, cfg: EvalConfig) -> dict:
    """合成回归：一组固定的既有测试文件，在子进程里跑。"""
    files = [str(settings.project_root / f) for f in cfg.synthetic_regression]
    missing = [f for f in cfg.synthetic_regression if not (settings.project_root / f).is_file()]
    if missing:
        return {"ok": False, "files": list(cfg.synthetic_regression), "summary": f"缺少测试文件：{missing}"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-p", "no:cacheprovider", "-o", "addopts=", "-q", *files],
                       cwd=settings.project_root, capture_output=True, text=True)
    tail = (r.stdout.strip().splitlines() or [""])[-1]
    return {"ok": r.returncode == 0, "files": list(cfg.synthetic_regression), "summary": tail[:200]}


def regression(settings: Settings, con: sqlite3.Connection, cfg: EvalConfig, *, registry=None, synthetic=None) -> dict:
    """全量回归：事故库加合成回归。任一失败就是失败。INCOMPLETE 的事故不算覆盖，单列。"""
    from ..incidents import library
    inc = library.run_all(settings, con, registry=registry)
    syn = (synthetic or synthetic_regression)(settings, cfg)
    return {"ok": inc["ok"] and syn["ok"], "incidents": inc, "synthetic": syn}


# ---------------------------------------------------------------- 评测库
def _eval_settings(settings: Settings, cfg: EvalConfig, reg: dict, caps: list[dict]) -> Settings:
    root = settings.holdout_root / "evaluation"
    return dataclasses.replace(settings, data_root=cfg.sealed_data_root or settings.data_root,
                               runtime_db=root / f"{reg['registry_id']}.sqlite",
                               dev_visible_through=date.fromisoformat(caps[-1]["end_day"]))


def open_eval_db(settings: Settings, con: sqlite3.Connection, cfg: EvalConfig, reg: dict):
    """评测方的数据库：导入封存行情到该登记最后一个胶囊的末日。增量；已导入的文件不重读。"""
    from ..pipeline import run_ingest
    caps = windows.capsules(con, reg["registry_id"])
    es = _eval_settings(settings, cfg, reg, caps)
    es.runtime_db.parent.mkdir(parents=True, exist_ok=True)
    econ = db.open_runtime(es)
    run_ingest(es, econ, write_report=False)
    row = con.execute("SELECT * FROM baselines WHERE baseline_id=?", (reg["baseline_id"],)).fetchone()
    with econ:
        econ.execute(f"INSERT OR IGNORE INTO baselines VALUES ({','.join('?' * len(row))})", tuple(row))
    return es, econ, caps


def verify_window(es: Settings, econ: sqlite3.Connection, root: str, caps: list[dict]) -> list[str]:
    """导入后的行情逐个胶囊核对内容摘要。返回对不上的胶囊。"""
    by_day = {d["day"]: d for d in splits.visible_days(econ, root)}
    bad = []
    for c in caps:
        days = [by_day.get(d[0]) for d in c["days"]]
        if any(d is None for d in days):
            # 登记时无数据的日子在库里也没有记录：按登记时的样子补上
            days = [by_day.get(d[0]) or {"day": d[0], "valid": False, "source": None, "contract_id": None, "sha": None}
                    for d in c["days"]]
        got = [[d["day"], int(bool(d["valid"]))] for d in days]
        want = [[d[0], d[1]] for d in c["days"]]
        seg = None
        if got == want and all(d["contract_id"] for d in days):
            seg = splits._segment(splits.CAPSULE, c["seq"], days, "SEALED")["content_sha256"]
        if seg != c["content_sha256"]:
            bad.append(f"capsule_{c['seq']}")
    return bad


def _back(day: str, n: int) -> str:
    d = date.fromisoformat(day)
    for _ in range(n):
        d = sessions.prev_trading_day(d)
    return d.isoformat()


def _arm(es, econ, scope, strategy, reg, stage, role, start, end, risk_cfg, engine_cfg, variant=None) -> tuple[str, list[dict]]:
    run_id = f"eval:{reg['registration_id']}:{stage}:{role}"
    bl._run(es, econ, scope, strategy, reg["baseline_id"], run_id, "EVAL_" + role.upper(), start, end,
            risk_cfg=risk_cfg, engine_cfg=engine_cfg, allow_real_data=True, variant=variant, keep="ledger")
    return run_id, metrics.load_trades(es, econ, scope, run_id, risk_cfg)


def _breakdown(es, econ, scope, bt, ct, days, unit, buckets) -> dict:
    """分列：入场时段、合约、当日波动（RTH 振幅三分位）。"""
    rng = {}
    for d in days:
        r = econ.execute("SELECT r.high, r.low FROM rth_days r JOIN trading_days t ON t.source=r.source AND"
                         " t.contract_id=r.contract_id AND t.trading_day=r.trading_day WHERE t.instrument_root=?"
                         " AND t.trading_day=?", (scope.instrument_root, d)).fetchone()
        if r and r[0] is not None:
            rng[d] = float(Decimal(str(r[0])) - Decimal(str(r[1])))
    order = sorted(rng.values())
    lo, hi = (order[len(order) // 3], order[2 * len(order) // 3]) if len(order) >= 3 else (None, None)

    def vol(t):
        v = rng.get(t["trading_day"])
        return "未知" if v is None or lo is None else ("低" if v < lo else "高" if v >= hi else "中")
    keys = {"入场时段": lambda t: metrics._bucket(t["entry_time_utc"], buckets), "合约": lambda t: t["contract_id"],
            "当日波动": vol}
    out = {}
    for name, fn in keys.items():
        g = {}
        for arm, ts in (("base", bt), ("cand", ct)):
            for t in ts:
                if t["trading_day"] not in set(days):
                    continue
                row = g.setdefault(fn(t), {"base_trades": 0, "base_net_r": 0.0, "cand_trades": 0, "cand_net_r": 0.0})
                row[f"{arm}_trades"] += 1
                row[f"{arm}_net_r"] += float(t["net_usd"]) / unit
        out[name] = dict(sorted(g.items()))
    return out


def measure(es, econ, scope, registry, reg, stage, window, cand, cfg, research_cfg, risk_cfg, engine_cfg) -> dict:
    """在评测库里回放两臂（从窗口首日前的预热日起），只度量窗口内的有效日。"""
    days = window["valid_days"]
    start = _back(window["start_day"], cfg.warmup_trading_days)
    diff = {d["key"]: d["to"] for d in cand["candidate"]["exact_diff"]}
    base_strategy = registry.get(scope.strategy_family)
    cand_strategy = registry.variant(scope.strategy_family, diff, cand["candidate_id"][-8:])
    kw = dict(risk_cfg=risk_cfg, engine_cfg=engine_cfg)
    _, bt = _arm(es, econ, scope, base_strategy, reg, stage, "base", start, window["end_day"], **kw)
    _, ct = _arm(es, econ, scope, cand_strategy, reg, stage, "cand", start, window["end_day"], **kw,
                 variant=Perturbation(cand["candidate_id"], reg["eval_track"], "candidate", diff))
    unit = risk_cfg.research_risk_unit_usd
    bs, cs = metrics.daily_series(bt, days, unit), metrics.daily_series(ct, days, unit)
    in_win = set(days)
    bw, cw = [t for t in bt if t["trading_day"] in in_win], [t for t in ct if t["trading_day"] in in_win]
    flags = sorted({f for t in cw for f in t.get("flags", ()) if f in ENGINE_FLAGS}
                   - {f for t in bw for f in t.get("flags", ()) if f in ENGINE_FLAGS})
    return {"arms": {"days": days, "base_net": bs["net"], "cand_net": cs["net"], "base_trades": bs["trades"],
                     "cand_trades": cs["trades"]},
            "base_trades": bw, "cand_trades": cw, "engine_flags_only_in_candidate": flags,
            "breakdown": _breakdown(es, econ, scope, bt, ct, days, float(unit), tuple(research_cfg.time_buckets_et)),
            "warmup_start": start}


# ---------------------------------------------------------------- 报告
def _protected_dir(settings: Settings) -> Path:
    return settings.holdout_root / "evaluation" / "reports"


def _write_protected(settings: Settings, evaluation_id: str, body: dict) -> str:
    d = _protected_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{evaluation_id}.json"
    p.write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
    (d / f"{evaluation_id}.md").write_text(render_full(body), encoding="utf-8")
    return str(p)


def _num(x) -> str:
    return "—" if x is None else (str(x) if isinstance(x, (int, bool)) else f"{x:.4f}")


def render_full(b: dict) -> str:
    """完整报告（只写在评测方目录）。"""
    L = [f"# 评测 {b['evaluation_id']}（{b['stage']}）", "",
         f"- 候选 `{b['candidate_id']}`　轨道 `{b['eval_track']}`　scope `{b['scope']}`",
         f"- 协议 `{b['protocol_id']}`　基线 `{b['baseline_id']}`　登记 `{b['registration_id']}`",
         f"- 窗口：{b['window']['start_day']} 至 {b['window']['end_day']}（胶囊 {b['window']['capsules']}，有效日 {b['result']['valid_days']}）",
         f"- 结论：**`{b['result']['status']}`**" + (f"（{'、'.join(b['result']['reasons'])}）" if b["result"]["reasons"] else ""), ""]
    d = b["result"].get("detail") or {}
    if d:
        L += ["## 两臂", "", "| | 基线 | 候选 |", "| --- | --- | --- |",
              f"| 净 R | {_num(d['base_net'])} | {_num(d['cand_net'])} |",
              f"| 成交数 | {d['base_trades']} | {d['cand_trades']} |",
              f"| 最大回撤（R） | {_num(d['base_max_drawdown'])} | {_num(d['cand_max_drawdown'])} |", "",
              f"配对净 R／日均差：{_num(d['mean_diff'])}（分母 {d['valid_days']} 个有效日，零交易日计入）", ""]
    if b["result"].get("gates"):
        L += ["## 门槛", "", "| 门槛 | 通过 | 数值 | 阈值 | 分母 | 说明 |", "| --- | --- | --- | --- | --- | --- |"]
        L += [f"| {g['gate']} | {'是' if g['passed'] else '否'} | {_num(g['value'])} | {_num(g['threshold'])} |"
              f" {_num(g['denominator'])} | {g['note'] or ''} |" for g in b["result"]["gates"]]
        bd = b["result"]["bound"]
        L += ["", f"置信界：区块 {bd['block']} 日，重采样 {bd['resamples']} 次，单侧 α={bd['alpha']:g}，下界 {_num(bd['bound'])}"
              + (f"（{bd['reason']}）" if bd["reason"] else ""), ""]
    if b.get("delta_trades"):
        t = b["delta_trades"]
        L += ["## 差异交易（辅助，不改变结论）", "",
              f"基线入场但候选拦截 {t['blocked_trades']} 笔，平均净 R {_num(t['blocked_mean_r'])}，单侧置信上界 {_num(t['upper_bound'])}；"
              f"候选独有 {t['candidate_only_trades']} 笔（路径差异）。", ""]
    for name, g in (b.get("breakdown") or {}).items():
        L += [f"## 分列：{name}", "", "| 组 | 基线笔数 | 基线净 R | 候选笔数 | 候选净 R |", "| --- | --- | --- | --- | --- |"]
        L += [f"| {k} | {v['base_trades']} | {_num(v['base_net_r'])} | {v['cand_trades']} | {_num(v['cand_net_r'])} |"
              for k, v in g.items()] + [""]
    rg = b.get("regression") or {}
    if rg:
        L += ["## 回归", "", f"事故库：{rg['incidents']['summary']}；合成回归：{rg['synthetic']['summary']}", ""]
    L += ["这是纸面模拟的评测，不是投资建议。评测只产出报告，发布由独立的发布入口处理。"]
    return "\n".join(L) + "\n"


def _visible(result: dict, fields) -> dict:
    return {k: result[k] for k in fields if k in result}


def _finish(settings, con, reg, stage, visible: dict, now: int, out_dir: Path | None) -> dict:
    eid = _eval_id(reg, stage)
    with con:
        con.execute("UPDATE evaluation_runs SET status=?, result_json=?, version=version+1 WHERE evaluation_id=?",
                    (visible["status"], canon(visible), eid))
    d = Path(out_dir) if out_dir else settings.project_root / "reports" / "evaluations"
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# 评测摘要 {eid}（{stage}）", "", f"- 候选 `{reg['candidate_id']}`　轨道 `{reg['eval_track']}`",
             f"- 协议 `{reg['protocol_id']}`　登记 `{reg['registration_id']}`",
             f"- 结论：**`{visible['status']}`**" + (f"（{'、'.join(visible.get('reasons') or [])}）" if visible.get("reasons") else "")]
    if "valid_days" in visible:
        lines.append(f"- 有效日：{visible['valid_days']}")
    if visible.get("gates_passed"):
        lines += ["", "| 门槛 | 通过 |", "| --- | --- |"] + [f"| {k} | {'是' if v else '否'} |" for k, v in visible["gates_passed"].items()]
    lines += ["", "这是协议允许给作者的摘要。逐日、逐笔和门槛数值只在评测方目录。", "这是纸面模拟的评测，不是投资建议。"]
    (d / f"{eid}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"evaluation_id": eid, "stage": stage, "candidate_id": reg["candidate_id"], **visible,
            "summary_report": str(d / f"{eid}.md")}


def _begin(con, reg, stage, now) -> None:
    """占住这个阶段：已有最终结果就拒绝（每段只看一次）。上次中断留下的 RUNNING 可以接着跑。"""
    row = stage_row(con, reg, stage)
    if row is not None and row["status"] != "RUNNING":
        raise EvalError("ALREADY_VIEWED", f"登记 {reg['registration_id']} 的 {stage} 已经有结果 {row['status']}，不能再看一次")
    if row is None:
        with con:
            con.execute("INSERT INTO evaluation_runs (evaluation_id, candidate_id, scope, protocol_id, stage, status,"
                        " created_at_utc) VALUES (?,?,?,?,?,'RUNNING',?)",
                        (_eval_id(reg, stage), reg["candidate_id"], reg["scope"], reg["protocol_id"], stage, now))


def _checked(settings, con, scope, candidate_id, cfg, user):
    """运行前的校验：登记、候选未改、协议未改、引擎未改、守卫。"""
    reg = registration(con, candidate_id)
    c = cands.get(con, candidate_id)
    if c is None or candidate_digest(c) != reg["candidate_sha256"]:
        raise EvalError("CANDIDATE_CHANGED", f"候选 {candidate_id} 在登记之后被改动过")
    if "proto_" + sha(reg["protocol"])[:16] != reg["protocol_id"]:
        raise EvalError("PROTOCOL_CHANGED", "协议正文与登记的标识不符")
    if reg["protocol"]["evaluation"] != cfg.body():
        raise EvalError("PROTOCOL_CHANGED", "config/evaluation.yaml 在登记之后改过；已登记的评测只能按登记时的协议运行")
    if bl.code_digest(settings.project_root) != reg["protocol"]["engine_sha256"]:
        raise EvalError("ENGINE_CHANGED", "引擎或策略代码与基线冻结时不同：先单独验收并重建基线")
    try:
        g = guard.verify(settings, cfg, user=user)
    except guard.GuardError as e:
        raise EvalError(e.reason, str(e)) from None
    return reg, c, g


def _return_confirm(con, reg, now, why: str) -> bool:
    if "UNSEALED" in windows.events(con, reg["registration_id"], CONFIRM) or \
            "RETURNED" in windows.events(con, reg["registration_id"], CONFIRM):
        return False
    caps = [c for c in windows.capsules(con, reg["registry_id"]) if c["seq"] in reg["confirm"]["capsules"]]
    with con:
        windows.record(con, reg["registry_id"], reg["instrument_root"], caps, CONFIRM, "RETURNED",
                       reg["registration_id"], reg["candidate_id"], now, {"reason": why})
    return True


def _stage(settings, con, scope, registry, candidate_id, stage, *, cfg, research_cfg, risk_cfg, engine_cfg, user,
           synthetic, out_dir, now) -> dict:
    cfg = cfg or load_eval(settings.project_root)
    research_cfg = research_cfg or load_research(settings.project_root)
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    now = int(time.time()) if now is None else now
    reg, cand, g = _checked(settings, con, scope, candidate_id, cfg, user)
    fam, power = reg["hypothesis_family"], research_cfg.power
    window = reg["screen"] if stage == SCREEN else reg["confirm"]
    purpose = windows.SCREEN if stage == SCREEN else windows.CONFIRM
    if stage == CONFIRM:
        s = stage_row(con, reg, SCREEN)
        if s is None or s["status"] != gates.CONTINUE:
            raise EvalError("SCREEN_REQUIRED", "正式确认只对盲筛为 SCREEN_CONTINUE 的登记运行")
    _begin(con, reg, stage, now)
    visible_fields = cfg.visible_screen if stage == SCREEN else cfg.visible_confirm
    eid = _eval_id(reg, stage)

    def done(result, extra=None):
        body = {"evaluation_id": eid, "stage": stage, "candidate_id": candidate_id, "eval_track": reg["eval_track"],
                "scope": reg["scope"], "protocol_id": reg["protocol_id"], "baseline_id": reg["baseline_id"],
                "registration_id": reg["registration_id"], "window": {k: window[k] for k in ("capsules", "start_day", "end_day")},
                "result": result, "guard": g, **(extra or {})}
        _write_protected(settings, eid, body)
        out = _finish(settings, con, reg, stage, _visible(result, visible_fields), now, out_dir)
        cands.set_status(con, candidate_id, CAND_STATUS[result["status"]], f"{stage}:{result['status']}", now=now)
        with con:
            cands.ledger(con, candidate_id, reg["scope"], fam, f"EVAL_{stage}", now=now,
                         detail={"evaluation_id": eid, "status": result["status"]})
        if result["status"] not in (gates.CONTINUE, gates.PASS) or stage == CONFIRM:
            out["confirm_window_returned"] = _return_confirm(con, reg, now, result["status"])
        return out

    if stage == CONFIRM and reg["protocol"]["power_eligibility"].get(reg["eval_track"]) != "ELIGIBLE":
        # 预登记的功效资格不够：不解封确认窗，不扣 α
        why = reg["protocol"]["power_eligibility"].get(reg["eval_track"])
        return done({"status": gates.INSUFFICIENT, "reasons": [f"POWER_ELIGIBILITY:{why}"], "valid_days": 0,
                     "gates_passed": {}})

    rg = regression(settings, con, cfg, registry=registry, synthetic=synthetic)
    if not rg["ok"]:
        # 回归失败：直接 FAIL，不解封窗口
        st = gates.REJECTED if stage == SCREEN else gates.FAIL
        return done({"status": st, "reasons": ["REGRESSION_FAILED"], "valid_days": 0, "gates_passed": {}},
                    {"regression": rg})

    if stage == CONFIRM:
        try:
            cands.register_formal_test(con, candidate_id, power, now=now)      # 解封之前扣一次正式检验
        except cands.EvolveError as e:
            if e.code != "ALREADY_REGISTERED":
                with con:
                    con.execute("DELETE FROM evaluation_runs WHERE evaluation_id=? AND status='RUNNING'", (eid,))
                raise EvalError(e.code, str(e)) from None

    es, econ, _ = open_eval_db(settings, con, cfg, reg)
    try:
        caps = [c for c in windows.capsules(con, reg["registry_id"]) if c["seq"] in window["capsules"]]
        if "UNSEALED" not in windows.events(con, reg["registration_id"], purpose):
            with con:
                windows.record(con, reg["registry_id"], reg["instrument_root"], caps, purpose, "UNSEALED",
                               reg["registration_id"], candidate_id, now, {"evaluation_id": eid})
        bad = verify_window(es, econ, reg["instrument_root"], caps)
        if bad:
            result = {"status": gates.INSUFFICIENT, "reasons": [f"WINDOW_CONTENT_MISMATCH:{','.join(bad)}"],
                      "valid_days": 0, "gates_passed": {}}
            extra = {"regression": rg}
        else:
            m = measure(es, econ, scope, registry, reg, stage, window, cand, cfg, research_cfg, risk_cfg, engine_cfg)
            unit = float(risk_cfg.research_risk_unit_usd)
            tick_value = float(settings.instrument(scope.instrument_root).tick_value_usd)
            stress = 2 * power.stress_extra_ticks_per_side * tick_value * risk_cfg.paper_quantity / unit
            th = reg["protocol"]["thresholds"]
            if stage == SCREEN:
                errors = [f"ENGINE_FLAG:{f}" for f in m["engine_flags_only_in_candidate"]]
                result = gates.screen(m["arms"], cfg, required_days=th["screen_required_days"], errors=errors)
            else:
                result = gates.confirm(m["arms"], reg["eval_track"], power, cfg, required_days=th["confirm_required_days"],
                                       stress_r_per_trade=stress, regression_ok=True)
            extra = {"regression": rg, "breakdown": m["breakdown"], "warmup_start": m["warmup_start"],
                     "daily": [{"day": d, "base_net_r": b, "cand_net_r": c, "base_trades": bn, "cand_trades": cn}
                               for d, b, c, bn, cn in zip(m["arms"]["days"], m["arms"]["base_net"], m["arms"]["cand_net"],
                                                          m["arms"]["base_trades"], m["arms"]["cand_trades"])],
                     "stress_r_per_trade": stress}
            if reg["eval_track"] == gates.TRACK_A:
                extra["delta_trades"] = stats.delta_trades(m["base_trades"], m["cand_trades"], unit, alpha=power.alpha,
                                                           seed=cfg.seed, resamples=cfg.resamples)
    finally:
        econ.close()
    if stage == CONFIRM:
        cands.consume_formal_test(con, candidate_id, power, now=now)
    return done(result, extra)


def screen(settings, con, scope, registry, candidate_id, *, cfg=None, research_cfg=None, risk_cfg=None, engine_cfg=None,
           user=None, synthetic=None, out_dir=None, now=None) -> dict:
    """阶段 A：盲筛。只淘汰，不出晋级结论。"""
    return _stage(settings, con, scope, registry, candidate_id, SCREEN, cfg=cfg, research_cfg=research_cfg,
                  risk_cfg=risk_cfg, engine_cfg=engine_cfg, user=user, synthetic=synthetic, out_dir=out_dir, now=now)


def confirm(settings, con, scope, registry, candidate_id, *, cfg=None, research_cfg=None, risk_cfg=None, engine_cfg=None,
            user=None, synthetic=None, out_dir=None, now=None) -> dict:
    """阶段 B：独立确认窗上的正式检验。只看一次。"""
    return _stage(settings, con, scope, registry, candidate_id, CONFIRM, cfg=cfg, research_cfg=research_cfg,
                  risk_cfg=risk_cfg, engine_cfg=engine_cfg, user=user, synthetic=synthetic, out_dir=out_dir, now=now)


# ---------------------------------------------------------------- 标注类
def labels(settings: Settings, con: sqlite3.Connection, candidate_id: str, path: Path | str, *, scope_text: str = "global",
           cfg: EvalConfig | None = None, registry=None, synthetic=None, now: int | None = None) -> dict:
    """解释／纪律类与 Memory 类：读人工标注文件，算识别率、误报率、引用正确率。文件内容只当数据。"""
    cfg = cfg or load_eval(settings.project_root)
    now = int(time.time()) if now is None else now
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        samples = raw["samples"]
        ok = raw.get("candidate_id") == candidate_id and isinstance(samples, list) and all(
            isinstance(x.get("sample_id"), str) and isinstance(x.get("expected"), bool) and isinstance(x.get("predicted"), bool)
            and isinstance(x.get("citations_total"), int) and isinstance(x.get("citations_correct"), int)
            and 0 <= x["citations_correct"] <= x["citations_total"] for x in samples)
        if not ok or len({x["sample_id"] for x in samples}) != len(samples):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise EvalError("LABELS_INVALID", "标注文件格式不对，或不是这个候选的") from None
    known = con.execute("SELECT 1 FROM candidates WHERE candidate_id=? UNION SELECT 1 FROM memory_candidates"
                        " WHERE memory_candidate_id=?", (candidate_id, candidate_id)).fetchone()
    if known is None:
        raise EvalError("NO_CANDIDATE", f"没有这个候选：{candidate_id}")
    eid = "eval_" + sha([candidate_id, LABELING, sha(samples)])[:16]
    if con.execute("SELECT 1 FROM evaluation_runs WHERE evaluation_id=?", (eid,)).fetchone():
        raise EvalError("ALREADY_VIEWED", "同一份标注已经评测过")
    rg = regression(settings, con, cfg, registry=registry, synthetic=synthetic)
    frozen = con.execute("SELECT manifest_json FROM baselines WHERE status='FROZEN' ORDER BY updated_at_utc DESC").fetchall()
    code = bl.code_digest(settings.project_root)
    unchanged = all(json.loads(r[0])["code_sha256"] == code for r in frozen)     # 没有基线时无可比对
    result = gates.labeling(samples, cfg, regression_ok=rg["ok"], logic_unchanged=unchanged)
    with con:
        con.execute("INSERT INTO evaluation_runs (evaluation_id, candidate_id, scope, protocol_id, stage, status,"
                    " result_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                    (eid, candidate_id, scope_text, "labeling:" + cfg.digest[:16], LABELING, result["status"],
                     canon({**result, "regression_ok": rg["ok"]}), now))
    return {"evaluation_id": eid, "candidate_id": candidate_id, **result}


# ---------------------------------------------------------------- 状态
def status(con: sqlite3.Connection, scope: Scope, research_cfg: ResearchConfig) -> dict:
    regs = []
    for r in con.execute("SELECT * FROM evaluation_registrations WHERE scope=? ORDER BY created_at_utc, registration_id",
                         (str(scope),)):
        stages = {s["stage"]: s["status"] for s in con.execute(
            "SELECT stage, status FROM evaluation_runs WHERE candidate_id=? AND protocol_id=?",
            (r["candidate_id"], r["protocol_id"]))}
        regs.append({"registration_id": r["registration_id"], "candidate_id": r["candidate_id"],
                     "eval_track": r["eval_track"], "protocol_id": r["protocol_id"],
                     "screen_window": json.loads(r["screen_json"])["capsules"],
                     "confirm_window": json.loads(r["confirm_json"])["capsules"],
                     "screen": stages.get(SCREEN), "confirm": stages.get(CONFIRM)})
    base = con.execute("SELECT registry_id FROM baselines WHERE scope=? AND status='FROZEN' ORDER BY updated_at_utc DESC"
                       " LIMIT 1", (str(scope),)).fetchone()
    caps = windows.capsules(con, base[0]) if base else []
    st = windows.state(con, scope.instrument_root, caps)
    count = {k: sum(v == k for v in st.values()) for k in ("FREE", "ASSIGNED", "UNSEALED")}
    return {"scope": str(scope), "registrations": regs, "capsules": {"total": len(caps), **count},
            "alpha": cands.alpha_summary(con, str(scope), research_cfg.power)}
