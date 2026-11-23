"""策略候选的队列：入队、过期与替换、效应估计、排序、前向预演。

排序公式和权重在 config/evolve.yaml 里预登记；这里只按公式算，谁都不能在运行时改权重。
效应估计只用开发段和向前验证段。前向预演只看可运行性，不报告盈亏。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date

from ..config import Scope, Settings
from ..engine import runner
from ..research import baseline as bl
from ..research import metrics, report, splits
from ..research.config import Perturbation, PowerConfig, ResearchConfig
from . import candidates as cands
from .aggregate import EvolveError, canon
from .config import STRATEGY_TRACKS, EvolveConfig

QUEUED, SUPERSEDED = "QUEUED", "SUPERSEDED"
ENGINE_FLAGS = ("UNPRICEABLE", "UNABLE_TO_FLATTEN", "INVALID_AFTER_FILL")


def base_release_of(baseline_id: str) -> str:
    """还没有发布时，候选的基准就是已冻结的基线。"""
    return f"baseline:{baseline_id}"


def enqueue(con: sqlite3.Connection, cand: dict, meta: dict, *, now: int | None = None) -> bool:
    """PROPOSED 的策略类候选进队列。其他类别、OBSERVATION、合成数据得到的候选都不进。"""
    if (cand["category"] != "strategy_rule" or cand["status"] != "PROPOSED" or cand["eval_track"] not in STRATEGY_TRACKS
            or meta.get("fixture")):
        return False
    now = now or int(time.time())
    inputs = {"evidence_days": len(cand["evidence_dates"]), "counter_share": meta.get("counter_share"),
              "bucket_share": meta.get("bucket_share"), "bucket_top": meta.get("bucket_top")}
    with con:
        cur = con.execute(
            "INSERT OR IGNORE INTO candidate_queue (candidate_id, scope, eval_track, status, base_release, inputs_json,"
            " enqueued_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?)",
            (cand["candidate_id"], cand["scope"], cand["eval_track"], QUEUED, cand["base_release"], canon(inputs), now, now))
        if cur.rowcount:
            cands.ledger(con, cand["candidate_id"], cand["scope"], cand["eval_track"], "QUEUED", now=now)
            return True
        # 已在队列里：只更新排序输入。被替换后对着新基准重新提出的，重新排队并清掉旧的效应估计
        con.execute("UPDATE candidate_queue SET inputs_json=?, updated_at_utc=? WHERE candidate_id=? AND status=?",
                    (canon(inputs), now, cand["candidate_id"], QUEUED))
        cur = con.execute("UPDATE candidate_queue SET status=?, base_release=?, inputs_json=?, effect_json=NULL,"
                          " score=NULL, enqueued_at_utc=?, updated_at_utc=? WHERE candidate_id=? AND status=?"
                          " AND base_release<>?", (QUEUED, cand["base_release"], canon(inputs), now, now,
                                                   cand["candidate_id"], SUPERSEDED, cand["base_release"]))
        if cur.rowcount:
            cands.ledger(con, cand["candidate_id"], cand["scope"], cand["eval_track"], "QUEUED", now=now,
                         detail={"reason": "REPROPOSED_ON_NEW_BASE"})
    return bool(cur.rowcount)


def supersede(con: sqlite3.Connection, scope_text: str, current_base: str, cfg: EvolveConfig, *,
              now: int | None = None) -> list[dict]:
    """排队超过 queue_ttl_days、或基准已被替换的候选转 SUPERSEDED。需要时重新提出。"""
    now = now or int(time.time())
    out = []
    for r in con.execute("SELECT candidate_id, base_release, enqueued_at_utc FROM candidate_queue WHERE scope=? AND"
                         " status=?", (scope_text, QUEUED)).fetchall():
        reason = ("BASE_RELEASE_REPLACED" if r["base_release"] != current_base
                  else "QUEUE_TTL_EXPIRED" if now - r["enqueued_at_utc"] > cfg.queue_ttl_days * 86400 else None)
        if reason is None:
            continue
        with con:
            con.execute("UPDATE candidate_queue SET status=?, updated_at_utc=? WHERE candidate_id=?",
                        (SUPERSEDED, now, r["candidate_id"]))
        cands.set_status(con, r["candidate_id"], SUPERSEDED, reason, now=now)
        out.append({"candidate_id": r["candidate_id"], "reason": reason})
    return out


# ---------------------------------------------------------------- 排序
def score(effect: dict | None, inputs: dict, track: str, cfg: EvolveConfig, power: PowerConfig) -> float | None:
    """排序分 = 效应项 × 证据天数权重 × 机制可信度。没有效应估计时为空。"""
    if not effect or effect.get("mean_diff_per_day") is None:
        return None
    m = effect["mean_diff_per_day"]
    eff = max(0.0, m + power.margin_r_per_day) if track == "PRUNING_DEFENSIVE" else max(0.0, m)
    weight = min(1.0, inputs.get("evidence_days", 0) / cfg.days_full_weight)
    share = inputs.get("counter_share")
    cred = cfg.unknown_counter_factor if share is None else 1.0 - share
    bucket = inputs.get("bucket_share")
    if bucket is not None and bucket >= cfg.single_bucket_share:
        cred *= cfg.single_bucket_factor
    return eff * weight * cred


def rank(con: sqlite3.Connection, scope_text: str, cfg: EvolveConfig, power: PowerConfig) -> list[dict]:
    """重算分数并排序：分数高的在前，没有估计的排最后，同分按候选标识。"""
    rows = []
    for r in con.execute("SELECT * FROM candidate_queue WHERE scope=? AND status=?", (scope_text, QUEUED)).fetchall():
        effect = json.loads(r["effect_json"]) if r["effect_json"] else None
        inputs = json.loads(r["inputs_json"])
        s = score(effect, inputs, r["eval_track"], cfg, power)
        if s != r["score"]:
            with con:
                con.execute("UPDATE candidate_queue SET score=? WHERE candidate_id=?", (s, r["candidate_id"]))
        rows.append({"candidate_id": r["candidate_id"], "eval_track": r["eval_track"], "score": s, "inputs": inputs,
                     "effect": effect, "base_release": r["base_release"], "enqueued_at_utc": r["enqueued_at_utc"]})
    rows.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0.0), x["candidate_id"]))
    for i, x in enumerate(rows, 1):
        x["rank"] = i
    return rows


# ---------------------------------------------------------------- 效应估计
def frozen_registry(con: sqlite3.Connection, base: dict) -> dict:
    """基线冻结时登记的切分。不重新计算：之后入库的日子不属于基线的任何一段。"""
    segs = [{"kind": r["kind"], "seq": r["seq"], "days": json.loads(r["days_json"])} for r in con.execute(
        "SELECT kind, seq, days_json FROM data_segments WHERE registry_id=? ORDER BY start_day, kind, seq",
        (base["registry_id"],))]
    if not segs:
        raise EvolveError("NO_REGISTRY", f"基线 {base['baseline_id']} 的切分登记不在库里")
    return {"registry_id": base["registry_id"], "segments": segs}


def _diff_of(c: dict) -> dict:
    return {d["key"]: d["to"] for d in c["candidate"]["exact_diff"]}


def estimate(settings: Settings, con: sqlite3.Connection, scope: Scope, registry, base: dict, cid: str, *,
             research_cfg: ResearchConfig, risk_cfg, engine_cfg, allow_real_data: bool = False,
             now: int | None = None) -> dict:
    """在开发段加向前验证段回放候选配置，与基线主运行逐日配对。计一次开发尝试。"""
    c = cands.get(con, cid)
    q = con.execute("SELECT status FROM candidate_queue WHERE candidate_id=?", (cid,)).fetchone()
    if c is None or q is None or q["status"] != QUEUED:
        raise EvolveError("NOT_QUEUED", f"候选 {cid} 不在队列里")
    reg = frozen_registry(con, base)
    days = report.measured_days(reg, research_cfg)
    every = days["total"]["all"]
    if not every:
        raise EvolveError("NO_DEV_DAYS", "没有开发段或向前验证段的数据")
    track = c["candidate"]["eval_track"]
    diff = _diff_of(c)
    strategy = registry.variant(scope.strategy_family, diff, cid[-8:])
    res = bl._run(settings, con, scope, strategy, base["baseline_id"], f"cand:{base['baseline_id']}:{cid}", "CANDIDATE",
                  every[0], every[-1], risk_cfg=risk_cfg, engine_cfg=engine_cfg, allow_real_data=allow_real_data,
                  variant=Perturbation(cid, track, "candidate", diff), keep="ledger")
    unit = risk_cfg.research_risk_unit_usd
    ct = metrics.load_trades(settings, con, scope, res["run_id"], risk_cfg)
    bt = metrics.load_trades(settings, con, scope, base["run_id"], risk_cfg)
    segs = {}
    for key in ("development", "forward_validation", "total"):
        valid = days[key]["valid"]
        if not valid:
            continue
        cs, bs = metrics.daily_series(ct, valid, unit), metrics.daily_series(bt, valid, unit)
        d = [a - b for a, b in zip(cs["net"], bs["net"])]
        segs[key] = {"days": len(valid), "mean_diff_per_day": sum(d) / len(d),
                     "base_net_per_day": sum(bs["net"]) / len(valid), "candidate_net_per_day": sum(cs["net"]) / len(valid),
                     "base_trades": sum(bs["trades"]), "candidate_trades": sum(cs["trades"]),
                     "base_max_drawdown": metrics.max_drawdown(bs["net"]),
                     "candidate_max_drawdown": metrics.max_drawdown(cs["net"]), "unit": cs["unit"]}
    total = segs.get("total") or {}
    effect = {"run_id": res["run_id"], "baseline_id": base["baseline_id"], "segments": segs,
              "mean_diff_per_day": total.get("mean_diff_per_day"), "unit": total.get("unit"),
              "note": "开发段与向前验证段上的描述性估计，用于排队，不是正式检验"}
    now = now or int(time.time())
    with con:
        con.execute("UPDATE candidate_queue SET effect_json=?, updated_at_utc=? WHERE candidate_id=?",
                    (canon(effect), now, cid))
        if not res["reused"]:
            cands.ledger(con, cid, c["scope"], track, "DEV_ATTEMPT", now=now,
                         detail={"run_id": res["run_id"], "mean_diff_per_day": effect["mean_diff_per_day"]})
    return {"candidate_id": cid, "reused": res["reused"], **effect}


# ---------------------------------------------------------------- 前向预演
def shadow_days(con: sqlite3.Connection, scope: Scope, base: dict) -> list[str]:
    """可用于预演的日期：已入库、有效、晚于基线的开发与向前验证段的末日，且不属于基线登记的任何胶囊或余量段。"""
    reg = frozen_registry(con, base)
    seen = {d[0] for s in reg["segments"] for d in s["days"]}
    used = [d[0] for s in reg["segments"] if s["kind"] in (splits.DEVELOPMENT, splits.FORWARD) for d in s["days"]]
    if not used:
        return []
    last = max(used)
    return [d["day"] for d in splits.visible_days(con, scope.instrument_root)
            if d["valid"] and d["day"] > last and d["day"] not in seen]


def pre_eval_shadow(settings: Settings, con: sqlite3.Connection, scope: Scope, registry, base: dict,
                    ranked: list[dict], *, cfg: EvolveConfig, risk_cfg, engine_cfg,
                    allow_real_data: bool = False, now: int | None = None) -> dict:
    """对排名前几的候选做可运行性检查。只记执行异常，不记盈亏；看过的日期登记下来，不得再进确认窗。"""
    days = shadow_days(con, scope, base)
    top = ranked[:cfg.shadow_top_n]
    if not days:
        return {"status": "NO_ELIGIBLE_DAYS", "candidates": [x["candidate_id"] for x in top], "days": [],
                "note": "运行库里没有晚于基线数据、且不属于胶囊的有效交易日"}
    now = now or int(time.time())
    out = []
    for x in top:
        cid = x["candidate_id"]
        done = {r[0] for r in con.execute("SELECT trading_day FROM pre_eval_shadow_days WHERE candidate_id=?", (cid,))}
        todo = [d for d in days if d not in done]
        if not todo:
            out.append({"candidate_id": cid, "observed_days": sorted(done), "new_days": [], "ok": None})
            continue
        c = cands.get(con, cid)
        run_id = f"shadow:{cid}:{todo[0]}_{todo[-1]}"
        error, per_day = None, {d: {"decisions": 0, "data_errors": 0, "engine_flags": 0} for d in todo}
        try:
            strategy = registry.variant(scope.strategy_family, _diff_of(c), cid[-8:])
            bl.purge_run(con, run_id)
            runner.replay(settings, con, scope, strategy, date.fromisoformat(todo[0]), date.fromisoformat(todo[-1]),
                          run_id=run_id, risk_cfg=risk_cfg, engine_cfg=engine_cfg, allow_real_data=allow_real_data)
            for status, dj in con.execute("SELECT signal_status, decision_json FROM scan_events WHERE run_id=?", (run_id,)):
                d = json.loads(dj).get("trading_day")
                if d in per_day:
                    per_day[d]["decisions"] += 1
                    per_day[d]["data_errors"] += status == "DATA_ERROR"
            for d, fj in con.execute("SELECT trading_day, flags_json FROM paper_trades WHERE run_id=?", (run_id,)):
                if d in per_day:
                    per_day[d]["engine_flags"] += sum(f in ENGINE_FLAGS for f in json.loads(fj))
        except Exception as e:  # noqa: BLE001 — 跑不起来本身就是预演要报告的事
            error = f"{type(e).__name__}: {e}"
        finally:
            bl.purge_run(con, run_id)                  # 预演不留账本：没有盈亏可看
        ok = error is None and all(v["decisions"] > 0 and v["engine_flags"] == 0 for v in per_day.values())
        with con:
            for d in todo:
                con.execute("INSERT OR IGNORE INTO pre_eval_shadow_days VALUES (?,?,?,?)",
                            (cid, d, canon({**per_day[d], "error": error}), now))
            cands.ledger(con, cid, c["scope"], c["candidate"]["eval_track"], "PRE_EVAL_SHADOW", now=now,
                         detail={"days": todo, "ok": ok, "error": error})
        out.append({"candidate_id": cid, "observed_days": sorted(done | set(todo)), "new_days": todo, "ok": ok,
                    "error": error, "per_day": per_day})
    return {"status": "DONE", "days": days, "results": out,
            "note": "这些日期已被候选的作者流程看过，不得再放进它们的正式确认窗"}
