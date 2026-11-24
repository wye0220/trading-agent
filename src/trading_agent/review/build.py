"""组装日复盘：事实 → 规则 → SessionReview（过 schema）→ 落库、落盘。相同输入得到相同的复盘。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

from .. import sessions
from ..config import Scope, Settings
from ..engine import llm
from ..engine.config import EngineConfig, RiskConfig, load_engine, load_risk
from ..manual import plans as plans_mod
from ..manual.config import ManualConfig, load_manual
from ..research import metrics
from ..research.config import ResearchConfig, load_research
from . import facts as facts_mod
from . import incidents, render, rules, seal

MAX_TEXT, MAX_ITEMS = 600, 30


class ReviewError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _cap(items: list[str], limit: int = MAX_ITEMS) -> list[str]:
    items = [s[:MAX_TEXT] for s in items if s]
    if len(items) > limit:
        items = items[:limit - 1] + [f"其余 {len(items) - limit + 1} 条见 JSON 明细。"]
    return items


def _et(epoch: int) -> str:
    return sessions.et(epoch).strftime("%H:%M")


def _strategy_track(f: dict, opp: dict, research_cfg: ResearchConfig, risk_cfg: RiskConfig, tick_value: Decimal) -> dict:
    ts, day = f["trades"], [f["trading_day"]]
    kw = dict(tick_value=tick_value, research_unit=risk_cfg.research_risk_unit_usd,
              stress_ticks_per_side=research_cfg.power.stress_extra_ticks_per_side)
    s = metrics.summarize(ts, day, **kw)
    family = f["scope"].split(":")[0]
    groups = {label: metrics.group_table(ts, day, lambda t, p=path: metrics._path(t["metadata"], p), **kw)
              for label, path in research_cfg.groups_for(family).items()}
    orders = Counter(o["status"] for o in f["orders"])
    return {"allowed": len(opp["allowed"]), "blocked": len(opp["blocked"]),
            "blocked_reasons": dict(Counter(b["reason"] for b in opp["blocked"])),
            "orders": dict(orders), "expired_orders": [o["order_id"] for o in f["orders"] if o["status"] == "EXPIRED"],
            "trades": s["trades"], "net_usd": s["net_usd"], "net_r": s["net_r_total"], "win_rate": s["win_rate"],
            "mae_ticks": s["mae_ticks"], "mfe_ticks": s["mfe_ticks"], "flags": s["flags"],
            "exit_reasons": s["exit_reasons"], "groups": {k: v for k, v in groups.items() if v}}


def _manual_track(f: dict) -> dict | None:
    man = f.get("manual")
    if not man:
        return None
    k = man["kpis"] or {}
    alerts = []
    per_rule = Counter(a["rule"] for a in man["alerts"])
    for a in man["alerts"]:
        handled = None if a["resolved_at_utc"] is None else a["resolved_at_utc"] - a["created_at_utc"]
        later = sum(1 for b in man["alerts"] if b["rule"] == a["rule"] and b["created_at_utc"] > a["created_at_utc"])
        alerts.append({"alert_id": a["alert_id"], "rule": a["rule"], "level": a.get("level"),
                       "message": a.get("message"), "raised_at_utc": a["created_at_utc"],
                       "seconds_to_resolve": handled,
                       "resolved_in_session": handled is not None and a["resolved_at_utc"] <= f["rth_close_utc"],
                       "repeated_after": later, "count_for_rule": per_rule[a["rule"]]})
    adherence = Counter((t["plan_adherence"] or "UNKNOWN") for t in man["trades"]
                        if t["effect"] == "CLOSE" and not t["quarantined"])
    alignment = Counter((t["strategy_alignment"] or "UNKNOWN") for t in man["trades"]
                        if t["effect"] == "CLOSE" and not t["quarantined"])
    decidable = adherence["COMPLIANT"] + adherence["VIOLATION"]
    return {"account_id": man["account_id"], "plan_coverage": k.get("plan_coverage"),
            "plan_coverage_threshold": k.get("plan_coverage_threshold"),
            "sample_insufficient": bool(k.get("sample_insufficient")),
            "opens": k.get("opens", 0), "opens_planned": k.get("opens_planned", 0),
            "opens_unplanned": k.get("opens_unplanned", 0), "opens_link_unknown": k.get("opens_link_unknown", 0),
            "closes": k.get("closes", 0), "plan_adherence": dict(adherence), "adherence_decidable": decidable,
            "adherence_unknown": adherence["UNKNOWN"],
            "violation_rate": None if not decidable else adherence["VIOLATION"] / decidable,
            "strategy_alignment": dict(alignment),
            "discipline_events": dict(Counter(d["type"] for d in man["discipline_events"])),
            "alerts": alerts, "quarantined_trades": k.get("quarantined_trades", 0),
            "realized_usd": str(sum((Decimal(t["realized_usd"] or "0") for t in man["trades"]
                                     if t["effect"] == "CLOSE" and not t["quarantined"]), Decimal(0)))}


def build(settings: Settings, con: sqlite3.Connection, scope: Scope, session: dict, *,
          research_cfg: ResearchConfig | None = None, risk_cfg: RiskConfig | None = None,
          manual_cfg: ManualConfig | None = None, account_id: str | None = None) -> dict:
    """只组装，不写任何东西。返回 {"review": SessionReview, "detail": …}。"""
    research_cfg = research_cfg or load_research(settings.project_root)
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    manual_cfg = manual_cfg or load_manual(settings.project_root)
    manual_only = scope is None
    if manual_only:
        f = facts_mod.load_manual_only(con, session, account_id, manual_cfg)
        tick_value = Decimal("0.01")
    else:
        f = facts_mod.load(settings, con, scope, session, risk_cfg, account_id=account_id, manual_cfg=manual_cfg)
        tick_value = settings.instrument(scope.instrument_root).tick_value_usd
    px = lambda t: rules._px(t, f["tick_size"])        # noqa: E731
    opp = rules.opportunities(f)
    hind = rules.hindsight(f)
    attr = rules.attribute(f)
    obs = rules.observations(f, opp, hind)
    strat = _strategy_track(f, opp, research_cfg, risk_cfg, tick_value)
    man = _manual_track(f)
    responses, memory = rules.respond_to_notes(f, lambda pid, at: plans_mod.was_active_at(con, pid, at))
    trig = incidents.triggers(f)
    day = f["day"] or {}

    # ---- 当时知道的事实
    known = [] if manual_only else [f"数据：来源 {day.get('source') or '无'}，实际合约 {day.get('contract_id') or '—'}，"
             f"RTH {day.get('rth_present', 0)}/{day.get('rth_expected', 0)} 根，"
             f"{'完整' if day.get('complete') else '不完整（' + str(day.get('reason')) + '）'}"
             f"{'，来源质量降级' if day.get('degraded') else ''}。决策记录 {len(f['events'])} 条。"]
    if not manual_only:
        known.append(f"机会：规则允许 {len(opp['allowed'])} 个（生成挂单），拦截 {len(opp['blocked'])} 个"
                     + ("：" + "、".join(f"{k}×{v}" for k, v in sorted(strat['blocked_reasons'].items()))
                        if strat["blocked_reasons"] else "") + "。")
    for a in opp["allowed"]:
        o = next((o for o in f["orders"] if o["order_id"] == a["order_id"]), {})
        known.append(f"{_et(a['as_of_utc'])} 信号 {a['setup'] or ''} {a['label'] or ''} {'做多' if a['side'] == 'LONG' else '做空'}，"
                     f"挂单触发 {px(o.get('trigger_ticks'))}、止损 {px(o.get('stop_ticks'))}，结果 {o.get('status')}"
                     f"{'（' + str(o.get('end_reason')) + '）' if o.get('status') != 'FILLED' else ''}。[{a['event_id']}]")
    for t in f["trades"]:
        known.append(f"交易 {'多' if t['side'] == 'LONG' else '空'} {_et(t['entry_time_utc'])} 入 {px(t['entry_ticks'])}，"
                     f"{_et(t['exit_time_utc'])} {t['exit_reason']} 出 {px(t['exit_ticks'])}，净 {t['net_usd']} 美元"
                     + (f"（{t['r_research']} R）" if t["r_research"] is not None else "")
                     + (f"，标记 {'、'.join(t['flags'])}" if t["flags"] else "") + f"。[{t.get('event_id') or t['trade_id']}]")
    if man:
        cov = man["plan_coverage"]
        known.append(f"人工轨（账户 {man['account_id']}）：开仓 {man['opens']} 次，其中有事前计划 {man['opens_planned']} 次，"
                     f"计划覆盖率 {'无成交' if cov is None else format(cov, '.0%')}"
                     + ("，低于阈值，纪律结论样本不足" if man["sample_insufficient"] else "")
                     + f"；平仓 {man['closes']} 次，已实现 {man['realized_usd']} 美元。")

    # ---- 证据不足的判断
    weak = []
    amb = Counter(x for t in f["trades"] for x in t["flags"] if x in rules.MAJOR_AMBIGUITY + rules.MINOR_AMBIGUITY)
    if amb:
        weak.append("成交歧义：" + "、".join(f"{k}×{v}" for k, v in sorted(amb.items()))
                    + "。这些成交的价格或是否成交依赖保守规则，不是行情唯一确定的。")
    coarse = sum(1 for t in f["trades"] if t["sub_bar_timeframe"] != "1m")
    if coarse:
        weak.append(f"{coarse} 笔交易只有 5 分钟子 K，同一根内的成交先后无法判定。")
    if manual_only:
        weak.append("这是只含人工轨的复盘：没有对应的策略 session，不评价与策略的关系，也没有任何行情。")
    if day and not day.get("complete"):
        weak.append("当日数据不完整，缺口附近的信号和成交不可靠。")
    if man:
        if man["adherence_unknown"]:
            weak.append(f"人工平仓 {man['adherence_unknown']} 次无法判定是否按计划（没有计划或没有订单事件），标为 UNKNOWN，"
                        f"不计入纪律比例的分母（可判定 {man['adherence_decidable']} 次）。")
        unk = man["strategy_alignment"].get("UNKNOWN", 0)
        if unk:
            weak.append(f"人工平仓 {unk} 次的策略一致性为 UNKNOWN（该品种没有正式策略，或当时没有可用的策略状态）。")
        if man["opens_link_unknown"]:
            weak.append(f"{man['opens_link_unknown']} 次开仓与计划只有时间吻合、没有凭据，关联状态待核对。")
        if man["sample_insufficient"]:
            weak.append("计划覆盖率低于阈值：当日的纪律结论样本不足，只作记录。")

    # ---- 事后信息
    later = []
    for h in hind:
        if h["mechanism"] == "EXPIRED_THEN_TRIGGERED":
            later.append(f"挂单 {h['order_id']} 到期后 {h['seconds_after_expiry'] // 60} 分钟，价格越过了触发价 {px(h['trigger_ticks'])}。")
        else:
            later.append(f"交易 {h['trade_id']} 止损出场后 {h['seconds_after_exit'] // 60} 分钟，价格到过原目标 {px(h['target_ticks'])}。")
    for t in f["trades"]:
        later.append(f"交易 {t['trade_id']}：持仓期间最大不利 {t['mae_ticks']} tick、最大有利 {t['mfe_ticks']} tick，"
                     f"持仓 {t['holding_seconds'] // 60} 分钟。")

    # ---- 观察
    review_obs = [{"kind": "OBSERVATION", "scope": f["scope"], "mechanism": rules.describe(o)[:200],
                   "evidence_event_ids": [e for e in o["evidence"] if e][:50]} for o in obs]
    if not review_obs:
        need = "当日没有交易、拦截或异常。" + ("" if account_id else "要评价人工轨，需要指定账户并导入当日的成交和订单事件。")
        review_obs = [{"kind": "NO_OBSERVATION", "scope": f["scope"], "mechanism": need[:200], "evidence_event_ids": []}]

    inc = [incidents.build(settings, con, f, t) for t in trig]
    review = {
        "session_id": f["session_id"], "scope": f["scope"], "trading_day": f["trading_day"],
        "plan_coverage": None if not man else man["plan_coverage"],
        "facts_known_then": _cap(known), "insufficient_evidence": _cap(weak), "hindsight": _cap(later),
        "attribution": [{"event_id": a["event_id"], "category": a["category"], "note": a["note"][:MAX_TEXT]}
                        for a in attr][:60],
        "observations": review_obs[:30],
        "trader_note_responses": [{"note_id": r["note_id"], "response": r["response"],
                                   "evidence_event_ids": r["evidence_event_ids"]} for r in responses],
        "incident_drafts": [i["incident_id"] for i in inc],
    }
    detail = {"rules_version": rules.RULES_VERSION, "run_id": f["run_id"], "run_mode": f["run_mode"],
              "account_id": account_id, "data": day, "opportunities": opp, "strategy_track": strat,
              "manual_track": man, "attribution": attr, "attribution_counts": dict(Counter(a["category"] for a in attr)),
              "observations": [{**o, "description": rules.describe(o)} for o in obs],
              "hindsight": hind, "note_responses": responses, "memory_candidates": memory,
              "incident_triggers": trig, "version_tuple": (f["events"][-1]["version_tuple"] if f["events"] else {}),
              "kpi": json.loads(session["kpi_json"]) if isinstance(session.get("kpi_json"), str) else session.get("kpi")}
    return {"review": review, "detail": detail, "_incidents": inc, "_facts": f}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def report_paths(settings: Settings, session_id: str, out_dir: Path | None = None) -> tuple[Path, Path]:
    d = Path(out_dir) if out_dir else settings.project_root / "reports" / "sessions"
    name = session_id.replace(":", "_").replace("/", "_")
    return d / f"{name}.md", d / f"{name}.json"


def run(settings: Settings, con: sqlite3.Connection, scope: Scope | None, run_id: str | None, trading_day: str, *,
        account_id: str | None = None, seal_first: bool = False, out_dir: Path | None = None,
        research_cfg: ResearchConfig | None = None, risk_cfg: RiskConfig | None = None,
        manual_cfg: ManualConfig | None = None, engine_cfg: EngineConfig | None = None) -> dict:
    """为一个已封存的 session 生成复盘并保存。seal_first 时先封存。"""
    from datetime import date
    manual_only = run_id is None
    if manual_only:
        if not account_id:
            raise ReviewError("CONFIG", "只复盘人工轨时必须指定账户")
        scope = None
        manual_cfg = manual_cfg or load_manual(settings.project_root)
        sid = seal.manual_session_id(account_id, trading_day)
        if seal_first:
            seal.seal_manual(settings, con, account_id, date.fromisoformat(trading_day), manual_cfg)
    else:
        from ..release.shadow import reserved_window_start
        hold = reserved_window_start(con, str(scope)) if scope is not None else None
        if hold and trading_day >= hold:
            raise ReviewError("FORWARD_HOLDOUT", f"{trading_day} 在进行中的前向保留窗口里（起于 {hold}）：影子结束前不复盘")
        sid = seal.session_id(run_id, trading_day)
        if seal_first:
            seal.seal(settings, con, scope, run_id, date.fromisoformat(trading_day), engine_cfg)
    row = seal.get(con, sid)
    if row is None or row["status"] != seal.SEALED:
        raise ReviewError("NOT_SEALED", f"session {sid} 没有封存，不能复盘")
    if not manual_only:
        cut = settings.dev_visible_through
        kpi = json.loads(row["kpi_json"])
        fixture = bool((kpi.get("data") or {}).get("fixture"))
        if not fixture and (cut is None or date.fromisoformat(trading_day) > cut):
            raise ReviewError("HOLDOUT_PROTECTED", f"{trading_day} 晚于开发可见截止日，不由普通复盘处理")
    scope_text = row["scope"]
    built = build(settings, con, scope, dict(row), research_cfg=research_cfg, risk_cfg=risk_cfg, manual_cfg=manual_cfg,
                  account_id=account_id)
    review, detail = built["review"], built["detail"]
    errs = llm.schema_errors(settings.project_root, "SessionReview", review)
    if errs:
        with con:
            con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, run_id, created_at_utc)"
                        " VALUES ('WARN','REVIEW_SCHEMA_INVALID',?,?,?,?,?)",
                        (scope_text, f"session {sid} 的复盘不符合 SessionReview schema", json.dumps(errs[:5], ensure_ascii=False),
                         run_id, int(time.time())))
        raise ReviewError("SCHEMA_INVALID", "复盘不符合 SessionReview schema：" + "；".join(errs[:3]))
    for m in detail["memory_candidates"]:
        merr = llm.schema_errors(settings.project_root, "MemoryCandidate", m["body"])
        if merr:
            raise ReviewError("SCHEMA_INVALID", "Memory 候选不符合 schema：" + "；".join(merr[:3]))

    saved_inc = [incidents.save(settings, con, body) for body in built["_incidents"]]
    detail["incident_drafts"] = saved_inc
    detail["candidates"] = _engine_candidates(con, scope_text, sid, trading_day, detail, saved_inc)
    sha = hashlib.sha256(_canon(review).encode()).hexdigest()
    now = int(time.time())
    summary = {"trades": detail["strategy_track"]["trades"], "net_usd": str(detail["strategy_track"]["net_usd"]),
               "allowed": detail["strategy_track"]["allowed"], "blocked": detail["strategy_track"]["blocked"],
               "attribution": detail["attribution_counts"], "observations": [o["mechanism"] for o in detail["observations"]],
               "incidents": len(saved_inc), "candidates": len(detail["candidates"]),
               "plan_coverage": review["plan_coverage"]}
    with con:
        old = con.execute("SELECT review_sha256, version, rules_version, updated_at_utc, superseded_json FROM"
                          " session_reviews WHERE session_id=?", (sid,)).fetchone()
        if old is None:
            con.execute("INSERT INTO session_reviews (session_id, scope, trading_day, account_id, review_json,"
                        " review_sha256, summary_json, rules_version, created_at_utc, updated_at_utc)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, scope_text, trading_day, account_id, _canon(review), sha, _canon(summary),
                         rules.RULES_VERSION, now, now))
        elif old["review_sha256"] != sha:
            hist = json.loads(old["superseded_json"]) + [{"sha256": old["review_sha256"], "version": old["version"],
                                                         "rules_version": old["rules_version"],
                                                         "at_utc": old["updated_at_utc"]}]
            con.execute("UPDATE session_reviews SET account_id=?, review_json=?, review_sha256=?, summary_json=?,"
                        " rules_version=?, superseded_json=?, version=version+1, updated_at_utc=? WHERE session_id=?",
                        (account_id, _canon(review), sha, _canon(summary), rules.RULES_VERSION, _canon(hist), now, sid))
        con.execute("DELETE FROM observations WHERE session_id=?", (sid,))
        for o in detail["observations"]:
            oid = "obs_" + hashlib.sha256(f"{sid}|{o['mechanism']}|{_canon(o['evidence'])}".encode()).hexdigest()[:20]
            con.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                        (oid, sid, scope_text, trading_day, o["track"], o["mechanism"], _canon(o["evidence"]),
                         _canon({"items": o["items"], "description": o["description"]}), now))
        for m in detail["memory_candidates"]:
            mid = "memc_" + hashlib.sha256(f"{m['note_id']}|{_canon(m['body'])}".encode()).hexdigest()[:20]
            con.execute("INSERT OR IGNORE INTO memory_candidates VALUES (?,?,?,?,'PENDING_CONFIRMATION',?)",
                        (mid, sid, m["note_id"], _canon(m["body"]), now))
    md_path, json_path = report_paths(settings, sid, out_dir)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render.markdown(review, detail), encoding="utf-8")
    json_path.write_text(json.dumps({"review": review, "detail": detail}, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    _enqueue_narration(settings, con, scope_text, sid, review, detail, engine_cfg)
    return {"ok": True, "session_id": sid, "trading_day": trading_day, "review_sha256": sha, **summary,
            "reports": {"markdown": str(md_path), "json": str(json_path)}}


def _engine_candidates(con, scope, sid: str, trading_day: str, detail: dict, saved_inc: list[dict]) -> list[dict]:
    """只有指向数据或引擎问题的观察可以直接立案。这不是策略候选：没有可评测的配置差异，走引擎修复流程。"""
    out = []
    now = int(time.time())
    for o in detail["observations"]:
        if o["track"] != "DATA_ENGINE":
            continue
        cid = "cand_" + hashlib.sha256(f"{scope}|{o['mechanism']}|{trading_day}".encode()).hexdigest()[:16]
        body = {"schema": "EngineFixRequest", "candidate_id": cid, "status": "PROPOSED", "scope": str(scope),
                "category": "data_engine", "eval_track": "CHEAP_NON_STRATEGY", "mechanism": o["mechanism"],
                "observed_problem": o["description"], "evidence_event_ids": [e for e in o["evidence"] if e][:50],
                "evidence_dates": [trading_day], "session_id": sid,
                "incident_ids": [i["incident_id"] for i in saved_inc],
                "flow": "ENGINE_FIX_FLOW：先修复并单独验收，冻结新代码后用同一版本重建基线。没有可由评测解释的配置差异。"}
        with con:
            cur = con.execute(
                "INSERT OR IGNORE INTO candidates (candidate_id, scope, category, eval_track, status, body_json,"
                " created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                (cid, str(scope), "data_engine", "CHEAP_NON_STRATEGY", "PROPOSED", _canon(body), now, now))
            if cur.rowcount:
                con.execute("INSERT INTO candidate_ledger (candidate_id, scope, hypothesis_family, event, from_status,"
                            " to_status, detail_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                            (cid, str(scope), "data_engine:" + o["mechanism"], "CREATED", None, "PROPOSED",
                             _canon({"session_id": sid, "source": "daily_review"}), now))
        out.append({"candidate_id": cid, "mechanism": o["mechanism"], "created": bool(cur.rowcount)})
    return out


def _enqueue_narration(settings, con, scope, sid, review, detail, engine_cfg) -> None:
    """把一份压缩的结构化上下文放进解释队列，供以后接入模型写叙述。本阶段没有消费者；放不进去不影响复盘。"""
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    ctx = {"kind": "SessionReview", "session_id": sid, "scope": str(scope), "trading_day": review["trading_day"],
           "plan_coverage": review["plan_coverage"], "attribution_counts": detail["attribution_counts"],
           "observations": [o["description"] for o in detail["observations"]][:12],
           "strategy": {k: detail["strategy_track"][k] for k in ("allowed", "blocked", "trades", "blocked_reasons")},
           "net_usd": str(detail["strategy_track"]["net_usd"]), "incidents": len(review["incident_drafts"])}
    try:
        if llm.estimate_tokens(ctx) <= engine_cfg.token_budgets["SessionReview"]:
            llm.enqueue_explanation(con, "SessionReview", ctx, scope=str(scope), event_id=sid)
    except Exception:  # noqa: BLE001
        pass
