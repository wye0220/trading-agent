"""evolve 命令的总编排：窗口 → 聚类 → 升级 → 候选 → 队列 → 周报。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..config import Scope, Settings
from ..engine.config import load_engine, load_risk
from ..research import baseline as bl
from ..research.config import ResearchConfig, load_research
from . import aggregate as agg
from . import candidates as cands
from . import queue
from .aggregate import EvolveError, canon
from .config import EvolveConfig, load_evolve


def frozen_baseline(con: sqlite3.Connection, scope_text: str) -> dict:
    r = con.execute("SELECT baseline_id FROM baselines WHERE scope=? AND status='FROZEN' AND run_id IS NOT NULL"
                    " ORDER BY updated_at_utc DESC LIMIT 1", (scope_text,)).fetchone()
    if r is None:
        raise EvolveError("NO_BASELINE", f"{scope_text} 没有已冻结的基线")
    return bl.get(con, r[0])


def _persist(con, evolve_id, scope_text, as_of, input_sha, clusters, now) -> None:
    with con:
        con.execute("INSERT INTO evolve_runs (evolve_id, scope, as_of_day, input_sha256, result, created_at_utc,"
                    " updated_at_utc) VALUES (?,?,?,?,'NO_CANDIDATE',?,?) ON CONFLICT(evolve_id) DO UPDATE SET"
                    " updated_at_utc=excluded.updated_at_utc", (evolve_id, scope_text, as_of, input_sha, now, now))
        con.execute("DELETE FROM observation_clusters WHERE evolve_id=?", (evolve_id,))
        for c in clusters:
            stats = {k: c[k] for k in ("track", "counter_share", "bucket", "note", "pooled_eligible", "fixture")}
            stats.update(first_day=c["days"][0], last_day=c["days"][-1], evidence_n=len(c["evidence"]),
                         group_scopes=c.get("group_scopes"))
            con.execute("INSERT INTO observation_clusters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (evolve_id, c["cluster_id"], c["scope"], c["strategy_family"], c["mechanism"], c["reason"],
                         c["diff_key"], len(c["days"]), c["occurrences"],
                         None if c["counter_days"] is None else len(c["counter_days"]), c["status"], c["group_id"],
                         canon(stats), canon(c["examples"])))


def _meta(c: dict, source: str, evolve_id: str, prior_failure: dict | None = None) -> dict:
    return {"source": source, "cluster_id": c["cluster_id"], "mechanism_key": cands.mechanism_key(c),
            "evolve_id": evolve_id, "counter_share": c["counter_share"], "bucket_share": c["bucket"]["share"],
            "bucket_top": c["bucket"]["top"], "group_id": c["group_id"], "group_scopes": c.get("group_scopes"),
            "pooled_eligible": c["pooled_eligible"], "fixture": c["fixture"], "prior_failure": prior_failure}


def run(settings: Settings, con: sqlite3.Connection, scope: Scope, registry, *, run_id: str | None = None,
        account_id: str | None = None, last_n: int | None = None, cfg: EvolveConfig | None = None,
        research_cfg: ResearchConfig | None = None, engine_cfg=None, proposals: list[dict] | None = None,
        out_dir: Path | None = None, candidates_dir: Path | None = None, now: int | None = None) -> dict:
    cfg = cfg or load_evolve(settings.project_root)
    research_cfg = research_cfg or load_research(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    now = now or int(time.time())
    scope_text = str(scope)
    base = frozen_baseline(con, scope_text)
    base_release = queue.base_release_of(base["baseline_id"])
    n = cfg.window_sessions if last_n is None else last_n
    wins = [agg.window(settings, con, scope_text, run_id or base["run_id"], n)]
    if not wins[0]["sessions"]:
        raise EvolveError("NO_SESSIONS", f"{scope_text} 没有已封存并已复盘的 session"
                          + (f"（排除了 {wins[0]['excluded']}）" if wins[0]["excluded"] else ""))
    if account_id:
        wins.append(agg.window(settings, con, f"manual:{account_id}", None, n))
    input_sha = agg.input_digest(wins, cfg)
    evolve_id = "ev_" + agg.sha(f"{scope_text}|{input_sha}")[:16]
    buckets = tuple(research_cfg.time_buckets_et)
    clusters = [c for w in wins for c in agg.clusters(con, cfg, w, buckets)]
    agg.assign_groups(clusters, agg.other_scope_clusters(con, scope_text))
    as_of = max(s["trading_day"] for s in wins[0]["sessions"])
    _persist(con, evolve_id, scope_text, as_of, input_sha, clusters, now)

    family = scope.strategy_family
    strategy = registry.get(family)
    keys = cands.evolvable_keys(strategy)
    promotable = [c for c in clusters if c["status"] == agg.PROMOTABLE]
    reduce_ids = [agg.enqueue_reduce(con, engine_cfg, c, keys if c["scope"] == scope_text else (),
                                     len(next(w for w in wins if w["scope"] == c["scope"])["sessions"]), input_sha)
                  for c in promotable]
    superseded = queue.supersede(con, scope_text, base_release, cfg, now=now)

    # 提案：模板在前（按聚类证据天数从多到少），导入的在后；共用校验和每家族的上限
    by_id = {c["cluster_id"]: c for c in clusters}
    pending = []
    for t in cfg.templates_for(family):
        c = next((x for x in promotable if x["scope"] == scope_text and x["mechanism"] == t.mechanism
                  and x["reason"] == t.reason and x["diff_key"] == t.key), None)
        if c is None:
            continue
        try:
            cand = cands.from_template(t, c, strategy=strategy, base_release=base_release, cfg=cfg,
                                       research_cfg=research_cfg, window_n=len(wins[0]["sessions"]))
        except cands.Rejected as e:
            pending.append((c, None, f"template:{t.id}", t.prior_failure, e))
            continue
        pending.append((c, cand, f"template:{t.id}", t.prior_failure, None))
    pending.sort(key=lambda p: (-len(p[0]["days"]), p[2]))
    for p in proposals or []:
        c = by_id.get(p.get("cluster_id"))
        cand = p.get("candidate")
        if isinstance(cand, dict):
            cand = {**cand, "base_release": base_release}
        pending.append((c, cand, "import", p.get("prior_failure"), None))

    created, rejected, results = 0, [], []
    for c, cand, source, prior, err in pending:
        try:
            if err is not None:
                raise err
            if not isinstance(cand, dict):
                raise cands.Rejected("SCHEMA_INVALID", "提案里没有 candidate 对象")
            ok = cands.validate(settings, con, cand, c, registry=registry, cfg=cfg, prior_failure=prior)
            exists = cands.get(con, ok["candidate_id"]) is not None
            if not exists and created >= cfg.max_candidates_per_family:
                raise cands.Rejected("LIMIT_REACHED", f"每个策略家族每次最多 {cfg.max_candidates_per_family} 个候选")
            meta = _meta(c, source, evolve_id, prior)
            saved = cands.save(settings, con, ok, meta, out_dir=candidates_dir, now=now)
            created += saved["action"] == "CREATED"
            queued = queue.enqueue(con, ok, meta, now=now) if saved["action"] in ("CREATED", "REFRESHED", "UNCHANGED", "REPROPOSED") else False
            d = ok["exact_diff"][0]
            results.append({**saved, "source": source, "eval_track": ok["eval_track"], "category": ok["category"],
                            "key": d["key"], "from": d["from"], "to": d["to"], "cluster_id": c["cluster_id"],
                            "evidence_days": len(ok["evidence_dates"]), "queued": queued})
        except cands.Rejected as e:
            rejected.append({"source": source, "cluster_id": c["cluster_id"] if c else None, "reason": e.reason,
                             "message": str(e)})

    memory = _memory_candidates(settings, con, wins, promotable, evolve_id, now)
    ranked = queue.rank(con, scope_text, cfg, research_cfg.power)
    result = "CANDIDATES" if results else "NO_CANDIDATE"
    summary = {
        "ok": True, "evolve_id": evolve_id, "scope": scope_text, "result": result, "as_of_day": as_of,
        "baseline_id": base["baseline_id"], "input_sha256": input_sha, "config_version": cfg.version,
        "windows": [{"scope": w["scope"], "run_id": w["run_id"], "sessions": len(w["sessions"]),
                     "first_day": w["sessions"][0]["trading_day"] if w["sessions"] else None,
                     "last_day": w["sessions"][-1]["trading_day"] if w["sessions"] else None,
                     "excluded": w["excluded"], "fixture": w["fixture"]} for w in wins],
        "clusters": [{"cluster_id": c["cluster_id"], "scope": c["scope"], "mechanism": c["mechanism"],
                      "reason": c["reason"] or None, "diff_key": c["diff_key"], "status": c["status"],
                      "days": len(c["days"]), "occurrences": c["occurrences"],
                      "counter_days": None if c["counter_days"] is None else len(c["counter_days"]),
                      "counter_share": c["counter_share"], "bucket_top": c["bucket"]["top"],
                      "bucket_share": c["bucket"]["share"], "group_id": c["group_id"], "note": c["note"]}
                     for c in clusters],
        "promotable": len(promotable), "reduce_requests": sum(r is not None for r in reduce_ids),
        "memory_candidates": memory, "candidates": results, "created": created, "rejected": rejected, "superseded": superseded, "queue": ranked,
        "alpha": cands.alpha_summary(con, scope_text, research_cfg.power),
    }
    with con:
        con.execute("UPDATE evolve_runs SET result=?, summary_json=?, updated_at_utc=? WHERE evolve_id=?",
                    (result, canon({k: summary[k] for k in ("result", "promotable", "created", "candidates", "rejected",
                                                             "superseded", "windows")}), now, evolve_id))
    summary["reports"] = write_report(settings, summary, out_dir)
    return summary


def _memory_candidates(settings, con, wins, promotable, evolve_id, now) -> dict:
    """可升级、有暴露条件的人工轨聚类 → discipline_pattern 的 Memory 候选。没通过 Memory 校验的按原因计数。"""
    from ..memory import items as mitems
    from ..memory import store as mstore
    from ..memory.registry import load_registry
    reg = load_registry(settings.project_root)
    out = {"created": [], "existing": 0, "skipped": {}}
    for c in promotable:
        if c["track"] != "MANUAL" or c["counter_days"] is None or c["fixture"]:
            continue
        win = next(w for w in wins if w["scope"] == c["scope"])
        n = len(c["days"]) + len(c["counter_days"])
        event = c["mechanism"].removeprefix("DISCIPLINE_").lower()
        body = {"kind": "discipline_pattern", "scope": "global", "valid_until": None,
                "source_event_ids": [e for e in c["evidence"] if mitems.schema_errors(
                    settings.project_root, "MemoryCandidate", _probe(e)) == []][:50],
                "statement": {"subject": "trader", "condition": f"event={event}", "metric": "occurrence_day_rate",
                              "value": round(len(c["days"]) / n, 4), "unit": "ratio", "sample_n": n,
                              "period": f"{win['sessions'][0]['trading_day']}..{win['sessions'][-1]['trading_day']}",
                              "source": "evolve_cluster"}}
        try:
            r = mstore.propose(settings, con, body, origin=evolve_id, reg=reg, now=now)
        except mitems.Invalid as e:
            out["skipped"][e.reason] = out["skipped"].get(e.reason, 0) + 1
            continue
        if r["created"]:
            out["created"].append(r["memory_candidate_id"])
        else:
            out["existing"] += 1
    return out


def _probe(evidence_id: str) -> dict:
    return {"kind": "user_preference", "scope": "global", "valid_until": None, "source_event_ids": [evidence_id],
            "statement": {"subject": "user_preference", "condition": None, "metric": "x", "value": 1, "unit": None,
                          "sample_n": None, "period": None, "source": "trader_note"}}


def load_proposals(path: Path | str) -> list[dict]:
    """导入的提案文件：{"proposals": [{"cluster_id", "candidate", "prior_failure"?}, ...]}。内容是数据，逐条校验。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise EvolveError("PROPOSALS_UNREADABLE", f"提案文件读不了：{e}") from None
    items = raw.get("proposals") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise EvolveError("PROPOSALS_UNREADABLE", '提案文件应为 {"proposals": [...]}')
    return items


# ---------------------------------------------------------------- 周报
def _num(x, nd=3) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def render(s: dict) -> str:
    w0 = s["windows"][0]
    lines = [f"# 聚合演化 {s['scope']}（截至 {s['as_of_day']}）", "",
             f"- 结果：`{s['result']}`　基线：`{s['baseline_id']}`　运行：`{s['evolve_id']}`",
             f"- 窗口：{w0['sessions']} 个已复盘的 session（{w0['first_day']} 至 {w0['last_day']}）"
             + (f"，排除 {w0['excluded']}" if w0["excluded"] else "") + ("　**FIXTURE**" if w0["fixture"] else "")]
    for w in s["windows"][1:]:
        lines.append(f"- 人工轨 `{w['scope']}`：{w['sessions']} 个 session")
    lines += [f"- 聚类 {len(s['clusters'])} 个，可升级 {s['promotable']} 个；新候选 {s['created']} 个，"
              f"拒绝 {len(s['rejected'])} 个，转 SUPERSEDED {len(s['superseded'])} 个", "",
              "聚类只说明某类现象出现过多少天，不说明规则错了。候选是待检验的假设。", "",
              "## 聚类", "", "| scope | 机制 | 原因 | 配置差异键 | 天数 | 次数 | 反例天数 | 反例占比 | 最集中的时段 | 状态 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for c in sorted(s["clusters"], key=lambda c: (c["scope"], -c["days"], c["mechanism"], c["reason"] or "")):
        bucket = "—" if c["bucket_top"] is None else f"{c['bucket_top']}（{_pct(c['bucket_share'])}）"
        lines.append(f"| {c['scope']} | {c['mechanism']} | {c['reason'] or '—'} | {c['diff_key'] or '无'} | {c['days']} |"
                     f" {c['occurrences']} | {'未知' if c['counter_days'] is None else c['counter_days']} |"
                     f" {_pct(c['counter_share'])} | {bucket} | {c['status']} |")
    lines += ["", "## 候选", ""]
    if not s["candidates"]:
        lines.append("NO_CANDIDATE：这次没有生成或刷新任何候选。")
    else:
        lines += ["| 候选 | 轨道 | 改动 | 证据天数 | 状态 | 本次 | 进队列 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for c in s["candidates"]:
            lines.append(f"| {c['candidate_id']} | {c['eval_track']} | `{c['key']}`：{json.dumps(c['from'], ensure_ascii=False)}"
                         f" → {json.dumps(c['to'], ensure_ascii=False)} | {c['evidence_days']} | {c['status']} |"
                         f" {c['action']} | {'是' if c['queued'] else '否'} |")
    if s["rejected"]:
        lines += ["", "被拒绝的提案：", ""] + [f"- {r['source']}：`{r['reason']}` {r['message']}" for r in s["rejected"]]
    lines += ["", "## 队列", ""]
    if not s["queue"]:
        lines.append("队列为空。")
    else:
        lines += ["| 名次 | 候选 | 轨道 | 分数 | 平均配对日差（R／日） | 证据天数 | 反例占比 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for q in s["queue"]:
            eff = q["effect"]["mean_diff_per_day"] if q["effect"] else None
            lines.append(f"| {q['rank']} | {q['candidate_id']} | {q['eval_track']} | {_num(q['score'], 4)} |"
                         f" {'未估计' if eff is None else _num(eff, 4)} | {q['inputs'].get('evidence_days')} |"
                         f" {_pct(q['inputs'].get('counter_share'))} |")
        lines += ["", "分数 = 效应项 × 证据天数权重 × 机制可信度，公式和权重在 `config/evolve.yaml` 预登记。"
                  "效应估计来自开发段和向前验证段，只用于排队，不是正式检验。"]
    m = s.get("memory_candidates") or {}
    if m.get("created") or m.get("existing") or m.get("skipped"):
        lines += ["", "## Memory 候选（人工轨）", "",
                  f"新增 {len(m['created'])} 条，已有 {m['existing']} 条"
                  + ("，跳过：" + "、".join(f"{k} {v}" for k, v in sorted(m["skipped"].items())) if m["skipped"] else "")
                  + "。用 `memory` 命令查看和确认。"]
    lines += ["", "## α 额度", "", "| 假设家族 | 开发尝试 | 已登记的正式检验 | 已消耗 | 还可登记 | 余下的 α |",
              "| --- | --- | --- | --- | --- | --- |"]
    for fam, a in s["alpha"].items():
        lines.append(f"| {fam} | {a['dev_attempts']} | {a['formal_registered']} | {a['formal_consumed']} |"
                     f" {a['formal_tests_left']} | {a['alpha_remaining']:g} |")
    lines += ["", "这是纸面模拟和执行记录的聚合，不是投资建议。", ""]
    return "\n".join(lines)


def write_report(settings: Settings, summary: dict, out_dir: Path | None = None) -> dict:
    d = Path(out_dir) if out_dir else settings.project_root / "reports" / "evolve"
    d.mkdir(parents=True, exist_ok=True)
    stem = f"evolve_{summary['scope'].replace(':', '_')}_{summary['as_of_day']}"
    md, js = d / f"{stem}.md", d / f"{stem}.json"
    md.write_text(render(summary), encoding="utf-8")
    js.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"markdown": str(md), "json": str(js)}


def estimate_queue(settings, con, scope, registry, *, cfg=None, research_cfg=None, risk_cfg=None, engine_cfg=None,
                   allow_real_data=False, log=lambda m: None) -> dict:
    """为队列里还没有效应估计的候选逐个回放并估计，然后重排。"""
    cfg = cfg or load_evolve(settings.project_root)
    research_cfg = research_cfg or load_research(settings.project_root)
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    base = frozen_baseline(con, str(scope))
    queue.supersede(con, str(scope), queue.base_release_of(base["baseline_id"]), cfg)
    done = []
    for q in queue.rank(con, str(scope), cfg, research_cfg.power):
        if q["effect"] is not None:
            continue
        log(f"估计候选 {q['candidate_id']}（{q['eval_track']}）")
        e = queue.estimate(settings, con, scope, registry, base, q["candidate_id"], research_cfg=research_cfg,
                           risk_cfg=risk_cfg, engine_cfg=engine_cfg, allow_real_data=allow_real_data)
        done.append({"candidate_id": e["candidate_id"], "mean_diff_per_day": e["mean_diff_per_day"],
                     "segments": e["segments"]})
    return {"ok": True, "scope": str(scope), "baseline_id": base["baseline_id"], "estimated": done,
            "queue": queue.rank(con, str(scope), cfg, research_cfg.power),
            "alpha": cands.alpha_summary(con, str(scope), research_cfg.power)}
