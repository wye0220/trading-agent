"""Map：把窗口内已复盘 session 的观察按 scope × 机制 × 原因 × 配置差异键聚类。

全部是确定性的 SQL 和纯函数。只读 sessions、session_reviews、observations 和基线主运行的账本；
不读行情，不读胶囊或保留集。这里只统计"发生过什么"，不判断规则对不对。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter

from ..config import Settings
from ..engine import llm
from ..research.metrics import _bucket
from .config import EvolveConfig

OBSERVATION, PROMOTABLE = "OBSERVATION", "PROMOTABLE"
SHADOW_MODES = ("pre_eval_shadow", "live_shadow")


class EvolveError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj) -> str:
    return hashlib.sha256((obj if isinstance(obj, str) else canon(obj)).encode()).hexdigest()


# 暴露条件：满足它却没有出现该机制的 session 是反例。没有登记的机制算不出反例。
EXPOSURE = {
    "EXPIRED_THEN_TRIGGERED": lambda s: s["expired"] > 0,       # 当日有到期订单
    "STOPPED_THEN_TARGET": lambda s: s["stops"] > 0,            # 当日有止损出场
    "FORCED_FLAT": lambda s: s["trades"] > 0,                   # 当日有交易
    "TWO_STOPS_HALT": lambda s: s["stops"] >= 2,                # 当日至少两次止损
}


def exposure(mechanism: str):
    if mechanism.startswith("DISCIPLINE_"):
        return lambda s: s["opens"] > 0                         # 人工轨：当日有开仓
    return EXPOSURE.get(mechanism)


def family_of(scope_text: str) -> str:
    return scope_text.split(":")[0]


# ---------------------------------------------------------------- 窗口
def window(settings: Settings, con: sqlite3.Connection, scope_text: str, run_id: str | None, last_n: int) -> dict:
    """最近 N 个已封存并已复盘的 session（N=0 取全部）。人工轨的 scope 写作 manual:<账户>。"""
    manual = family_of(scope_text) == "manual"
    if manual:
        where, args = "s.session_id LIKE ? AND s.run_mode='manual_actual'", [scope_text + ":%"]
    else:
        where, args = "s.scope=? AND s.run_id=?", [scope_text, run_id]
    rows = con.execute(
        "SELECT s.session_id, s.trading_day, s.run_mode, s.run_id, s.kpi_json, r.review_sha256 FROM sessions s"
        " JOIN session_reviews r ON r.session_id = s.session_id WHERE s.status='SEALED' AND " + where
        + " ORDER BY s.trading_day DESC, s.session_id DESC", args).fetchall()
    cut = settings.dev_visible_through.isoformat() if settings.dev_visible_through else None
    from ..release.shadow import reserved_window_start
    hold = None if manual else reserved_window_start(con, scope_text)   # 进行中的前向保留窗口（阶段 9）
    used, excluded = [], Counter()
    for r in rows:
        kpi = json.loads(r["kpi_json"] or "{}")
        fixture = r["run_mode"] == "fixture" or bool((kpi.get("data") or {}).get("fixture"))
        if r["run_mode"] in SHADOW_MODES:
            excluded["SHADOW_WINDOW"] += 1
        elif hold and r["trading_day"] >= hold:
            excluded["FORWARD_HOLDOUT"] += 1                   # 结果解封前不进作者的聚合输入
        elif not manual and not fixture and (cut is None or r["trading_day"] > cut):
            excluded["AFTER_DEV_CUTOFF"] += 1                  # 截止日之后（或没设截止日）的真实数据不进作者输入
        else:
            if last_n and len(used) >= last_n:
                break
            used.append({"session_id": r["session_id"], "trading_day": r["trading_day"], "run_id": r["run_id"],
                         "review_sha256": r["review_sha256"], "fixture": fixture, "kpi": kpi,
                         "expired": int((kpi.get("orders_by_status") or {}).get("EXPIRED", 0)),
                         "trades": int(kpi.get("trades") or 0), "opens": int(kpi.get("opens") or 0), "stops": 0})
    used.sort(key=lambda s: (s["trading_day"], s["session_id"]))
    if used and not manual:
        days = {s["trading_day"]: s for s in used}
        for d, n in con.execute("SELECT trading_day, COUNT(*) FROM paper_trades WHERE run_id=? AND exit_reason='STOP'"
                                " AND trading_day BETWEEN ? AND ? GROUP BY 1",
                                (run_id, used[0]["trading_day"], used[-1]["trading_day"])):
            if d in days:
                days[d]["stops"] = n
    return {"scope": scope_text, "run_id": run_id, "sessions": used, "excluded": dict(excluded),
            "fixture": bool(used) and any(s["fixture"] for s in used)}


def _event_times(con, run_id, sessions) -> dict[str, int]:
    """证据事件的决策时点，用来看证据是否集中在单一入场时段。"""
    if not run_id or not sessions:
        return {}
    from datetime import date

    from .. import sessions as S
    lo = S.eth_bounds(date.fromisoformat(sessions[0]["trading_day"]))[0]
    hi = S.eth_bounds(date.fromisoformat(sessions[-1]["trading_day"]))[1]
    return dict(con.execute("SELECT event_id, as_of_utc FROM scan_events WHERE run_id=? AND as_of_utc BETWEEN ? AND ?",
                            (run_id, lo, hi)))


# ---------------------------------------------------------------- 聚类
def cluster_id(scope_text: str, mechanism: str, reason: str, diff_key: str | None) -> str:
    return "cl_" + sha(f"{scope_text}|{mechanism}|{reason}|{diff_key or ''}")[:16]


def _pick(items: list, n: int) -> list:
    """确定性地取最多 n 个：首、中、尾。"""
    if len(items) <= n:
        return list(items)
    idx = sorted({0, len(items) // 2, len(items) - 1})[:n]
    return [items[i] for i in idx]


def clusters(con: sqlite3.Connection, cfg: EvolveConfig, win: dict, time_buckets: tuple[str, ...]) -> list[dict]:
    scope_text, sess = win["scope"], win["sessions"]
    if not sess:
        return []
    family = family_of(scope_text)
    by_sid = {s["session_id"]: s for s in sess}
    con.execute("CREATE TEMP TABLE IF NOT EXISTS evolve_window (session_id TEXT PRIMARY KEY)")
    con.execute("DELETE FROM evolve_window")
    con.executemany("INSERT INTO evolve_window VALUES (?)", [(s["session_id"],) for s in sess])
    rows = con.execute(
        "SELECT o.observation_id, o.session_id, o.trading_day, o.mechanism, o.track, o.evidence_json, o.detail_json,"
        " COALESCE(json_extract(j.value, '$.reason'), '') AS reason, COUNT(*) AS n"
        " FROM observations o JOIN evolve_window w ON w.session_id = o.session_id"
        " LEFT JOIN json_each(o.detail_json, '$.items') j"
        " WHERE o.scope=? GROUP BY o.observation_id, reason ORDER BY o.trading_day, o.observation_id, reason",
        (scope_text,)).fetchall()
    con.execute("DELETE FROM evolve_window")
    times = _event_times(con, win["run_id"], sess)
    acc: dict[tuple, dict] = {}
    for r in rows:
        key = (r["mechanism"], r["reason"])
        c = acc.setdefault(key, {"track": r["track"], "obs": [], "occurrences": 0, "evidence": {}})
        c["occurrences"] += r["n"]
        detail = json.loads(r["detail_json"] or "{}")
        items = [i for i in detail.get("items", []) if (i.get("reason") or "") == r["reason"]]
        c["obs"].append({"trading_day": r["trading_day"], "session_id": r["session_id"],
                         "description": detail.get("description"), "items": items[:3]})
        for e in json.loads(r["evidence_json"] or "[]"):
            if e:
                c["evidence"].setdefault(e, r["trading_day"])
    out = []
    for (mech, reason), c in sorted(acc.items()):
        days = sorted({o["trading_day"] for o in c["obs"]})
        seen = {o["session_id"] for o in c["obs"]}
        exp = exposure(mech)
        counter = None if exp is None else [s for s in sess if s["session_id"] not in seen and exp(s)]
        share = None
        if counter is not None and (len(days) + len(counter)):
            share = len(counter) / (len(days) + len(counter))
        buckets = Counter(_bucket(times[e], time_buckets) for e in c["evidence"] if e in times)
        top = max(sorted(buckets), key=lambda b: buckets[b]) if buckets else None
        diff_key = cfg.diff_key(family, mech, reason)
        status = PROMOTABLE if len(days) >= cfg.min_days else OBSERVATION
        out.append({
            "cluster_id": cluster_id(scope_text, mech, reason, diff_key), "scope": scope_text, "strategy_family": family,
            "mechanism": mech, "reason": reason, "diff_key": diff_key, "track": c["track"], "status": status,
            "days": days, "occurrences": c["occurrences"], "evidence": sorted(c["evidence"]),
            "counter_days": None if counter is None else [s["trading_day"] for s in counter],
            "counter_sessions": None if counter is None else [s["session_id"] for s in counter],
            "counter_share": share,
            "bucket": {"top": top, "share": (buckets[top] / sum(buckets.values())) if top else None,
                       "known": sum(buckets.values()), "counts": dict(sorted(buckets.items()))},
            "note": None if diff_key else "没有白名单内的配置键可以表达这个机制；要改需要走人工代码分支",
            "group_id": None, "pooled_eligible": False, "fixture": win["fixture"],
            "examples": {
                "positive": _pick(c["obs"], cfg.examples_per_side),
                "counter": [] if counter is None else [
                    {"trading_day": s["trading_day"], "session_id": s["session_id"],
                     "kpi": {k: s["kpi"].get(k) for k in ("trades", "orders_by_status", "net_r", "opens") if k in s["kpi"]}}
                    for s in _pick(counter, cfg.examples_per_side)]},
        })
    return out


# ---------------------------------------------------------------- 跨品种候选组
def assign_groups(mine: list[dict], others: list[dict]) -> None:
    """同家族、同机制、同原因、同差异键的可升级聚类出现在别的品种时，标为一个候选组（各 scope 仍分别评测）。

    others 是别的 scope 最近一次运行的聚类（至少带 scope、strategy_family、mechanism、reason、diff_key、status）。
    """
    def root(scope_text):
        parts = scope_text.split(":")
        return parts[1] if len(parts) > 1 else ""

    for c in mine:
        if c["status"] != PROMOTABLE or not c["diff_key"]:
            continue
        peers = [o for o in others if o["status"] == PROMOTABLE and o["scope"] != c["scope"]
                 and root(o["scope"]) != root(c["scope"])
                 and (o["strategy_family"], o["mechanism"], o["reason"], o["diff_key"])
                 == (c["strategy_family"], c["mechanism"], c["reason"], c["diff_key"])]
        if peers:
            c["group_id"] = "grp_" + sha(f"{c['strategy_family']}|{c['mechanism']}|{c['reason']}|{c['diff_key']}")[:16]
            c["group_scopes"] = sorted({c["scope"], *(o["scope"] for o in peers)})
            c["pooled_eligible"] = not c["diff_key"].startswith("groups.")     # 品种专属的键不能合并检验


def other_scope_clusters(con: sqlite3.Connection, scope_text: str) -> list[dict]:
    """同一策略家族的其他 scope 各自最近一次运行的聚类。"""
    fam = family_of(scope_text)
    rows = con.execute(
        "SELECT c.scope, c.strategy_family, c.mechanism, c.reason, c.diff_key, c.status FROM observation_clusters c"
        " JOIN evolve_runs e ON e.evolve_id = c.evolve_id WHERE c.strategy_family=? AND c.scope<>?"
        " AND e.updated_at_utc = (SELECT MAX(updated_at_utc) FROM evolve_runs WHERE scope = c.scope)",
        (fam, scope_text)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 归纳输入（Reduce）
def reduce_payload(c: dict, allowed_keys: tuple[str, ...], window_n: int) -> dict:
    """送去归纳的内容：统计摘要加最多 3 条正例、3 条反例。不带原始 session 文本，也不带全部证据。"""
    return {"kind": "CandidateReduce", "cluster_id": c["cluster_id"], "scope": c["scope"], "mechanism": c["mechanism"],
            "reason": c["reason"] or None, "diff_key": c["diff_key"],
            "stats": {"window_sessions": window_n, "days": len(c["days"]), "occurrences": c["occurrences"],
                      "first_day": c["days"][0], "last_day": c["days"][-1],
                      "counter_days": None if c["counter_days"] is None else len(c["counter_days"]),
                      "counter_share": c["counter_share"], "time_bucket": c["bucket"]},
            "examples": c["examples"], "allowed_keys": list(allowed_keys),
            "constraints": "只改一个白名单内的键；不得触及风险上限、权限或评测协议；输出须通过 Candidate schema"}


def enqueue_reduce(con: sqlite3.Connection, engine_cfg, c: dict, allowed_keys, window_n: int, input_sha: str) -> str | None:
    """把一个可升级聚类的归纳输入放进解释队列。本阶段没有消费者；超出 token 预算的不入队。"""
    payload = reduce_payload(c, allowed_keys, window_n)
    if llm.estimate_tokens(payload) > engine_cfg.token_budgets["Candidate"]:
        return None
    return llm.enqueue_explanation(con, "Candidate", payload, scope=c["scope"], event_id=c["cluster_id"],
                                   snapshot_id=input_sha[:16])


def input_digest(wins: list[dict], cfg: EvolveConfig) -> str:
    return sha({"config": cfg.version,
                "windows": [{"scope": w["scope"], "run_id": w["run_id"],
                             "sessions": [[s["session_id"], s["review_sha256"]] for s in w["sessions"]]} for w in wins]})
