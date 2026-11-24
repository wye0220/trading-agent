"""阶段 6B：跨日聚合、候选台账、队列。"""
import copy
import json
import pathlib
from datetime import date

import pytest
import yaml

import helpers as H
import test_engine_runner as ER
import test_research as TR
from trading_agent.cli import main
from trading_agent.config import ConfigError
from trading_agent.engine import llm
from trading_agent.evolve import aggregate as A
from trading_agent.evolve import candidates as C
from trading_agent.evolve import queue as Q
from trading_agent.evolve import run as EV
from trading_agent.evolve.config import load_evolve, parse
from trading_agent.research import baseline as BL
from trading_agent.review import build as B

SCOPE = "pa_brooks:ES:RTH:5m"
RAW = yaml.safe_load((H.TEST_PROJECT / "config" / "evolve.yaml").read_text(encoding="utf-8"))
DAYS = TR.days_from(date(2025, 11, 17), 5)
KEY = "params.entry.stop_offset_ticks"
TPL = {"id": "t1", "mechanism": "TWO_STOPS_HALT", "track": "ALPHA_EXPANSION", "key": KEY, "to": 2,
       "observed_problem": "两次止损后停手", "hypothesis": "止损放远 1 tick 后净 R 更高", "expected_tradeoff": "单笔风险变大",
       "invariants": ["目标规则不变"]}


def ecfg(templates=(TPL,), mapping=(("TWO_STOPS_HALT", "", KEY),), **over):
    raw = copy.deepcopy(RAW)
    raw["mechanism_map"] = {"pa_brooks": [{"mechanism": m, "reason": r, "diff_key": k} for m, r, k in mapping]}
    raw["templates"] = {"pa_brooks": [dict(t) for t in templates]}
    for k, v in over.items():
        node = raw
        *head, last = k.split(".")
        for h in head:
            node = node[h]
        node[last] = v
    return parse(raw)


def reviewed(env, days=DAYS, fwd=None):
    """合成数据：冻结基线，把每个交易日封存并复盘。每天都是 Spike → 两次止损 → 停手。"""
    TR.fixture_env(env, days)
    rc = TR.cfg(**{"splits.forward_validation_start": (fwd or days[-1]).isoformat(), "splits.warmup_trading_days": 0})
    base = BL.freeze(env.settings, env.con, env.scope, TR.REG.get("pa_brooks"), rc, risk_cfg=ER.RISK, engine_cfg=ER.ENG)
    for d in days:
        B.run(env.settings, env.con, env.scope, base["run_id"], d.isoformat(), out_dir=env.tmp / "sessions",
              seal_first=True, risk_cfg=ER.RISK, engine_cfg=ER.ENG)
    return base, rc


def evolve(env, rc, cfg=None, **kw):
    return EV.run(env.settings, env.con, env.scope, TR.REG, cfg=cfg or ecfg(), research_cfg=rc, engine_cfg=ER.ENG,
                  out_dir=env.tmp / "evolve", candidates_dir=env.tmp / "candidates", **kw)


def seed(env, day, mech, *, scope=SCOPE, run_id="r1", reason=None, evidence=None, kpi=None, mode="fixture", n=1):
    """直接写一个已封存、已复盘的 session 和一条观察（mech 为 None 时只写 session）。"""
    sid = f"{run_id}:{day}"
    kpi = {"data": {"fixture": mode == "fixture"}, **(kpi or {})}
    env.con.execute("INSERT OR IGNORE INTO sessions (session_id, scope, trading_day, run_mode, risk_domain, run_id, status,"
                    " kpi_json, created_at_utc) VALUES (?,?,?,?,?,?,'SEALED',?,0)",
                    (sid, scope, day, mode, "d", run_id, json.dumps(kpi)))
    env.con.execute("INSERT OR IGNORE INTO session_reviews VALUES (?,?,?,NULL,'{}',?,'{}','v','[]',1,0,0)",
                    (sid, scope, day, "sha-" + sid))
    if mech:
        items = [{"reason": reason} if reason else {} for _ in range(n)]
        env.con.execute("INSERT INTO observations VALUES (?,?,?,?,'STRATEGY',?,?,?,0)",
                        (f"o:{sid}:{mech}:{reason}", sid, scope, day, mech, json.dumps(evidence or [f"ev:{sid}:{mech}"]),
                         json.dumps({"description": f"{mech} {day}", "items": items})))
    env.con.commit()
    return sid


def win(env, cfg=None, last_n=0, scope=SCOPE, run_id="r1"):
    w = A.window(env.settings, env.con, scope, run_id, last_n)
    return w, A.clusters(env.con, cfg or ecfg(), w, ("09:30", "12:00"))


def by_mech(cl):
    return {(c["mechanism"], c["reason"]): c for c in cl}


# ---------------------------------------------------------------- 配置
def test_config_loads_and_rejects_bad_values():
    cfg = load_evolve(H.TEST_PROJECT)
    assert cfg.min_days == 3 and cfg.max_candidates_per_family == 3 and cfg.queue_ttl_days == 120
    assert cfg.examples_per_side == 3 and cfg.version.startswith("evolve-")
    assert cfg.diff_key("pa_brooks", "FILTERED_SETUP", "TAIL_WINDOW_UNCONFIGURED") == "params.discipline.tail_window_minutes"
    assert cfg.diff_key("pa_brooks", "FILTERED_SETUP", "OTHER") is None and cfg.diff_key("orb", "FORCED_FLAT", "") is None
    for t in cfg.templates_for("pa_brooks"):          # 仓库里登记的模板都能通过白名单构造出变体
        TR.REG.variant("pa_brooks", {t.key: t.to}, t.id)
    assert ecfg(examples_per_side=9).examples_per_side == 3          # 规格上限
    for bad in ({"min_days": 0}, {"queue_score.single_bucket_share": 2}, {"window_sessions": "x"}):
        with pytest.raises(ConfigError):
            ecfg(**bad)
    with pytest.raises(ConfigError):
        ecfg(templates=({**TPL, "track": "NOPE"},))
    with pytest.raises(ConfigError):
        ecfg(templates=(TPL, TPL))


# ---------------------------------------------------------------- 窗口
def test_window_excludes_post_cutoff_and_shadow_sessions(tmp_path):
    from conftest import Env
    env = Env(tmp_path, dev_visible_through="2024-12-31")
    seed(env, "2024-12-30", "FORCED_FLAT", mode="historical_replay")
    seed(env, "2025-01-02", "FORCED_FLAT", mode="historical_replay")       # 截止日之后：不进作者输入
    seed(env, "2024-12-27", "FORCED_FLAT", mode="pre_eval_shadow")
    seed(env, "2024-12-26", "FORCED_FLAT", run_id="other", mode="historical_replay")
    w, cl = win(env)
    assert [s["trading_day"] for s in w["sessions"]] == ["2024-12-30"]
    assert w["excluded"] == {"AFTER_DEV_CUTOFF": 1, "SHADOW_WINDOW": 1} and not w["fixture"]
    assert [c["days"] for c in cl] == [["2024-12-30"]]
    env.con.close()


def test_window_takes_most_recent_n(env):
    for d in ("2025-11-17", "2025-11-18", "2025-11-19", "2025-11-20"):
        seed(env, d, "FORCED_FLAT")
    w, _ = win(env, last_n=2)
    assert [s["trading_day"] for s in w["sessions"]] == ["2025-11-19", "2025-11-20"] and w["fixture"]
    assert len(win(env, last_n=0)[0]["sessions"]) == 4
    env.con.execute("DELETE FROM session_reviews WHERE session_id='r1:2025-11-20'")      # 没复盘的不算
    assert [s["trading_day"] for s in win(env, last_n=2)[0]["sessions"]] == ["2025-11-18", "2025-11-19"]


# ---------------------------------------------------------------- 聚类
def test_clusters_split_by_reason_and_never_mix_scopes(env):
    for d, reason in (("2025-11-17", "A"), ("2025-11-18", "A"), ("2025-11-19", "B")):
        seed(env, d, "FILTERED_SETUP", reason=reason, n=2)
    seed(env, "2025-11-17", "FILTERED_SETUP", scope="pa_brooks:NQ:RTH:5m", run_id="nq", reason="A")
    w, cl = win(env, ecfg(mapping=(("FILTERED_SETUP", "A", KEY),)))
    m = by_mech(cl)
    assert set(m) == {("FILTERED_SETUP", "A"), ("FILTERED_SETUP", "B")}
    a, b = m[("FILTERED_SETUP", "A")], m[("FILTERED_SETUP", "B")]
    assert (a["days"], a["occurrences"], a["diff_key"], a["status"]) == (["2025-11-17", "2025-11-18"], 4, KEY, "OBSERVATION")
    assert b["diff_key"] is None and "白名单" in b["note"] and a["cluster_id"] != b["cluster_id"]
    assert all(c["scope"] == SCOPE and c["strategy_family"] == "pa_brooks" for c in cl)
    assert a["counter_days"] is None and a["counter_share"] is None                  # 这个机制算不出反例
    _, nq = win(env, scope="pa_brooks:NQ:RTH:5m", run_id="nq")
    assert [c["days"] for c in nq] == [["2025-11-17"]] and nq[0]["cluster_id"] not in {a["cluster_id"], b["cluster_id"]}


def test_clusters_are_deterministic(env):
    for d in ("2025-11-17", "2025-11-18", "2025-11-19"):
        seed(env, d, "FORCED_FLAT", kpi={"trades": 1})
    (w1, c1), (w2, c2) = win(env), win(env)
    assert A.canon(c1) == A.canon(c2) and A.input_digest([w1], ecfg()) == A.input_digest([w2], ecfg())
    assert A.input_digest([w1], ecfg()) != A.input_digest([w1], ecfg(min_days=2))       # 配置进输入摘要
    env.con.execute("UPDATE session_reviews SET review_sha256='changed' WHERE session_id='r1:2025-11-18'")
    assert A.input_digest([win(env)[0]], ecfg()) != A.input_digest([w1], ecfg())


def test_counterexamples_follow_exposure(env):
    seed(env, "2025-11-17", "EXPIRED_THEN_TRIGGERED", kpi={"orders_by_status": {"EXPIRED": 2}})
    seed(env, "2025-11-18", None, kpi={"orders_by_status": {"EXPIRED": 1}})        # 有到期订单，之后没有越过触发价
    seed(env, "2025-11-19", None, kpi={"orders_by_status": {"FILLED": 1}})         # 没有到期订单：不是反例
    seed(env, "2025-11-20", "DISCIPLINE_UNPLANNED", kpi={"opens": 1})
    c = by_mech(win(env)[1])
    e = c[("EXPIRED_THEN_TRIGGERED", "")]
    assert e["counter_days"] == ["2025-11-18"] and e["counter_share"] == 0.5
    assert e["examples"]["counter"][0]["session_id"] == "r1:2025-11-18"
    assert A.exposure("DISCIPLINE_UNPLANNED")({"opens": 1}) and A.exposure("AMBIGUOUS_FILL") is None
    assert c[("DISCIPLINE_UNPLANNED", "")]["counter_days"] == []


def test_stop_exposure_reads_paper_trades(env):
    base, rc = reviewed(env)
    w = A.window(env.settings, env.con, SCOPE, base["run_id"], 0)
    assert [s["stops"] for s in w["sessions"]] == [2] * 5 and all(s["trades"] == 2 for s in w["sessions"])
    c = by_mech(A.clusters(env.con, ecfg(), w, ("09:30", "12:00")))[("TWO_STOPS_HALT", "")]
    assert c["counter_days"] == [] and c["bucket"]["top"] == "09:30–12:00" and c["bucket"]["share"] == 1.0
    assert c["fixture"] and len(c["evidence"]) == 5


def test_promotion_needs_min_days_and_examples_are_capped(env):
    for i in range(10):
        seed(env, f"2025-11-{i + 3:02d}", "FORCED_FLAT", kpi={"trades": 1})
    seed(env, "2025-11-20", "STOPPED_THEN_TARGET")
    seed(env, "2025-11-21", "STOPPED_THEN_TARGET")
    c = by_mech(win(env)[1])
    assert c[("FORCED_FLAT", "")]["status"] == "PROMOTABLE" and c[("STOPPED_THEN_TARGET", "")]["status"] == "OBSERVATION"
    ex = c[("FORCED_FLAT", "")]["examples"]["positive"]
    assert [e["trading_day"] for e in ex] == ["2025-11-03", "2025-11-08", "2025-11-12"]      # 首、中、尾
    p = A.reduce_payload(c[("FORCED_FLAT", "")], ("a.b",), 12)
    assert len(p["examples"]["positive"]) == 3 and len(p["examples"]["counter"]) <= 3 and "evidence" not in p
    assert p["stats"]["days"] == 10 and p["allowed_keys"] == ["a.b"]
    assert by_mech(win(env, ecfg(min_days=2))[1])[("STOPPED_THEN_TARGET", "")]["status"] == "PROMOTABLE"


def test_cross_instrument_groups():
    def cl(scope, key=KEY, status="PROMOTABLE", mech="FORCED_FLAT"):
        return {"scope": scope, "strategy_family": scope.split(":")[0], "mechanism": mech, "reason": "", "diff_key": key,
                "status": status, "group_id": None, "pooled_eligible": False}
    es, es2 = cl(SCOPE), cl(SCOPE, "groups.ES_MES.max_retrace_pct")
    others = [cl("pa_brooks:NQ:RTH:5m"), cl("pa_brooks:NQ:RTH:5m", "groups.ES_MES.max_retrace_pct"),
              cl("orb:NQ:RTH:5m"), cl("pa_brooks:ES:ETH:5m")]
    A.assign_groups([es, es2], others)
    assert es["group_id"].startswith("grp_") and es["group_scopes"] == ["pa_brooks:ES:RTH:5m", "pa_brooks:NQ:RTH:5m"]
    assert es["pooled_eligible"] and es2["group_id"] != es["group_id"] and not es2["pooled_eligible"]   # 品种专属键不合并
    lone = cl(SCOPE)
    A.assign_groups([lone], [cl("orb:NQ:RTH:5m"), cl("pa_brooks:NQ:RTH:5m", status="OBSERVATION")])
    assert lone["group_id"] is None                                    # 别的家族、没到升级门槛的都不算


def test_reduce_requests_are_enqueued_once(env):
    base, rc = reviewed(env)
    r = evolve(env, rc, cfg=ecfg(templates=()))
    assert r["result"] == "NO_CANDIDATE" and r["promotable"] == 2 and r["reduce_requests"] == 2 and r["created"] == 0
    rows = env.con.execute("SELECT kind, status, context_json FROM explanation_requests WHERE kind='Candidate'").fetchall()
    assert len(rows) == 2 and all(x["status"] == "PENDING" for x in rows)
    ctx = json.loads(rows[0]["context_json"])
    assert ctx["kind"] == "CandidateReduce" and len(ctx["examples"]["positive"]) <= 3
    assert llm.estimate_tokens(ctx) <= ER.ENG.token_budgets["Candidate"]
    evolve(env, rc, cfg=ecfg(templates=()))
    assert env.con.execute("SELECT COUNT(*) FROM explanation_requests WHERE kind='Candidate'").fetchone()[0] == 2
    assert env.con.execute("SELECT COUNT(*) FROM evolve_runs").fetchone()[0] == 1          # 同样的输入：同一次运行


# ---------------------------------------------------------------- 候选
def test_template_candidate_is_created_once(env):
    base, rc = reviewed(env)
    r = evolve(env, rc)
    assert r["result"] == "CANDIDATES" and r["created"] == 1 and r["rejected"] == []
    c = r["candidates"][0]
    assert (c["action"], c["status"], c["key"], c["from"], c["to"], c["evidence_days"]) == ("CREATED", "PROPOSED", KEY, 1, 2, 5)
    assert c["queued"] is False and r["queue"] == []                    # 合成数据得到的候选不进队列
    saved = C.get(env.con, c["candidate_id"])
    cand, meta = saved["candidate"], saved["meta"]
    assert llm.schema_errors(H.TEST_PROJECT, "Candidate", cand) == []
    assert cand["base_release"] == f"baseline:{base['baseline_id']}" and cand["eval_track"] == "ALPHA_EXPANSION"
    assert cand["minimum_effect"] == rc.power.delta_r_per_day and cand["primary_metric"] == "net_r_per_day"
    assert cand["evaluation_protocol_id"].startswith("blind") and "20 个有效交易日" in cand["rollback_trigger"]
    assert meta["source"] == "template:t1" and meta["fixture"] and meta["mechanism_key"] == f"TWO_STOPS_HALT||{KEY}"
    md = pathlib.Path(c["files"]["markdown"]).read_text(encoding="utf-8")
    assert "只改这一个键" in md and "FIXTURE" in md and "不是结论" in md
    assert json.loads(pathlib.Path(c["files"]["json"]).read_text(encoding="utf-8"))["candidate"] == cand
    led = [x["event"] for x in env.con.execute("SELECT event FROM candidate_ledger WHERE candidate_id=?", (c["candidate_id"],))]
    assert led == ["CREATED"]
    again = evolve(env, rc)
    assert again["created"] == 0 and again["candidates"][0]["action"] == "UNCHANGED" and again["evolve_id"] == r["evolve_id"]
    assert env.con.execute("SELECT COUNT(*) FROM candidates WHERE category='strategy_rule'").fetchone()[0] == 1
    assert env.con.execute("SELECT COUNT(*) FROM candidate_ledger WHERE candidate_id=?", (c["candidate_id"],)).fetchone()[0] == 1
    md = pathlib.Path(r["reports"]["markdown"]).read_text(encoding="utf-8")
    for needle in ("FIXTURE", "TWO_STOPS_HALT", "α 额度", "不是投资建议", c["candidate_id"]):
        assert needle in md


def test_no_candidate_below_min_days(env):
    base, rc = reviewed(env, DAYS[:2], fwd=DAYS[1])
    r = evolve(env, rc)
    assert r["result"] == "NO_CANDIDATE" and r["promotable"] == 0 and r["candidates"] == []
    assert env.con.execute("SELECT COUNT(*) FROM candidates WHERE category='strategy_rule'").fetchone()[0] == 0
    assert {c["status"] for c in r["clusters"]} == {"OBSERVATION"}


def test_limit_three_candidates_per_family(env):
    """一次运行最多新生成 3 个：导入四个不同键的提案，只收前三个。"""
    base, rc = reviewed(env)
    keys = [KEY, "params.entry.trigger_offset_ticks", "params.entry.limit_offset_ticks", "params.pullback.max_label"]
    props = []
    for k in keys:
        c, cand = proposal(env, rc, base, key=k, **{"from": C.current_value(TR.REG.get("pa_brooks"), k), "to": 2})
        props.append({"cluster_id": c["cluster_id"], "candidate": cand})
    r = evolve(env, rc, cfg=ecfg(templates=()), proposals=props)
    assert r["created"] == 3 and [x["reason"] for x in r["rejected"]] == ["LIMIT_REACHED"]
    assert all(x["source"] == "import" and x["status"] == "PROPOSED" for x in r["candidates"])
    assert [x["key"] for x in r["candidates"]] == keys[:3]
    again = evolve(env, rc, cfg=ecfg(templates=()), proposals=props)      # 已有的不占名额，第四个这次能进
    assert again["created"] == 1 and again["rejected"] == []


def proposal(env, rc, base, **over):
    w = A.window(env.settings, env.con, SCOPE, base["run_id"], 0)
    c = by_mech(A.clusters(env.con, ecfg(), w, ()))[("TWO_STOPS_HALT", "")]
    cand = C.from_template(ecfg().templates_for("pa_brooks")[0], c, strategy=TR.REG.get("pa_brooks"),
                           base_release="baseline:x", cfg=ecfg(), research_cfg=rc, window_n=5)
    diff = {**cand["exact_diff"][0], **{k: v for k, v in over.items() if k in ("key", "from", "to", "target")}}
    cand = {**cand, **{k: v for k, v in over.items() if k not in diff}, "exact_diff": [diff]}
    return c, cand


def reason_of(env, cand, cluster, **kw):
    with pytest.raises(C.Rejected) as e:
        C.validate(env.settings, env.con, cand, cluster, registry=TR.REG, cfg=ecfg(), **kw)
    return e.value.reason


def test_validation_rejections(env):
    base, rc = reviewed(env)
    c, good = proposal(env, rc, base)
    ok = C.validate(env.settings, env.con, good, c, registry=TR.REG, cfg=ecfg())
    assert ok["status"] == "PROPOSED" and ok["candidate_id"].startswith("cand_")
    assert reason_of(env, proposal(env, rc, base, key="params.discipline.max_stops_per_day", **{"from": 2, "to": 3})[1], c) \
        == "NOT_WHITELISTED"                                              # 风控类的键不在白名单
    assert reason_of(env, proposal(env, rc, base, **{"from": 5})[1], c) == "FROM_MISMATCH"
    assert reason_of(env, proposal(env, rc, base, to=1)[1], c) == "NO_CHANGE"
    assert reason_of(env, proposal(env, rc, base, to="abc")[1], c) == "INVALID_VARIANT"
    assert reason_of(env, proposal(env, rc, base, target="rule_switch")[1], c) == "TARGET_INVALID"
    assert reason_of(env, proposal(env, rc, base, eval_track="CHEAP_NON_STRATEGY")[1], c) == "TRACK_INVALID"
    assert reason_of(env, proposal(env, rc, base, category="data_engine")[1], c) == "CATEGORY_NOT_SUPPORTED"
    assert reason_of(env, proposal(env, rc, base, category="explanation_discipline", eval_track="CHEAP_NON_STRATEGY",
                                   target="rule_switch")[1], c) == "NOT_WHITELISTED"
    assert reason_of(env, proposal(env, rc, base, evidence_event_ids=["made-up"])[1], c) == "EVIDENCE_NOT_IN_CLUSTER"
    assert reason_of(env, proposal(env, rc, base, evidence_dates=["2020-01-01"])[1], c) == "EVIDENCE_NOT_IN_CLUSTER"
    assert reason_of(env, good, None) == "EVIDENCE_NOT_IN_CLUSTER"
    assert reason_of(env, proposal(env, rc, base, scope="pa_brooks:NQ:RTH:5m")[1], c) == "SCOPE_MISMATCH"
    assert reason_of(env, {**good, "risk_limit": 1}, c) == "SCHEMA_INVALID"
    two = {**good, "exact_diff": good["exact_diff"] * 2}
    assert reason_of(env, two, c) == "SCHEMA_INVALID"                      # 一个候选只改一个键
    few = C.validate(env.settings, env.con, {**good, "evidence_dates": good["evidence_dates"][:2]}, c, registry=TR.REG,
                     cfg=ecfg())
    assert few["status"] == "OBSERVATION"                                 # 证据不够：留作观察
    assert Q.enqueue(env.con, few, {"fixture": False}) is False


def test_array_valued_diff_passes_schema_and_whitelist(env):
    base, rc = reviewed(env)
    key = "setups.spike_pullback.exempt"
    now = C.current_value(TR.REG.get("pa_brooks"), key)
    assert now == ["h1l1_block", "counter_htf", "range_position", "exhaustion"]
    c, cand = proposal(env, rc, base, key=key, **{"from": now, "to": now[1:]}, eval_track="PRUNING_DEFENSIVE")
    ok = C.validate(env.settings, env.con, cand, c, registry=TR.REG, cfg=ecfg())
    assert ok["status"] == "PROPOSED" and ok["exact_diff"][0]["to"] == now[1:]
    with pytest.raises(C.Rejected) as e:
        C.current_value(TR.REG.get("pa_brooks"), "params.nope.x")
    assert e.value.reason == "KEY_NOT_FOUND"


def test_reproposal_after_failure_must_reference_it(env):
    base, rc = reviewed(env)
    r = evolve(env, rc)
    cid = r["candidates"][0]["candidate_id"]
    assert C.set_status(env.con, cid, "FAIL", "EVALUATION_FAILED") and not C.set_status(env.con, cid, "FAIL", "again")
    c, same = proposal(env, rc, base)
    assert reason_of(env, same, c) == "ALREADY_FAILED"
    c, other = proposal(env, rc, base, to=3)
    assert reason_of(env, other, c) == "PRIOR_FAILURE_NOT_REFERENCED"
    assert reason_of(env, other, c, prior_failure={"candidate_id": "cand_x", "report": "r", "difference": "d"}) \
        == "PRIOR_FAILURE_NOT_REFERENCED"
    ok = C.validate(env.settings, env.con, other, c, registry=TR.REG, cfg=ecfg(),
                    prior_failure={"candidate_id": cid, "report": "reports/eval/x.md", "difference": "止损放到 3 tick"})
    assert ok["candidate_id"] != cid
    again = evolve(env, rc)                                               # 失败的候选保留原状，模板不会把它复活
    assert again["created"] == 0 and [x["reason"] for x in again["rejected"]] == ["ALREADY_FAILED"]
    assert C.get(env.con, cid)["status"] == "FAIL"
    led = [(x["event"], x["from_status"], x["to_status"]) for x in
           env.con.execute("SELECT event, from_status, to_status FROM candidate_ledger WHERE candidate_id=? ORDER BY entry_id",
                           (cid,))]
    assert led == [("CREATED", None, "PROPOSED"), ("STATUS_CHANGED", "PROPOSED", "FAIL")]


def test_alpha_budget(env):
    base, rc = reviewed(env)
    c, _ = proposal(env, rc, base)
    ids = []
    for to in (2, 3, 4, 5, 6, 7):
        cand = C.validate(env.settings, env.con, proposal(env, rc, base, to=to)[1], c, registry=TR.REG, cfg=ecfg())
        C.save(env.settings, env.con, cand, {"source": "t"}, out_dir=env.tmp / "c")
        ids.append(cand["candidate_id"])
    p = rc.power
    a = C.alpha_summary(env.con, SCOPE, p)["ALPHA_EXPANSION"]
    assert (a["formal_registered"], a["formal_tests_left"], a["alpha_remaining"]) == (0, p.max_formal_tests, p.alpha_family)
    for i, cid in enumerate(ids[:5]):
        assert C.register_formal_test(env.con, cid, p)["formal_tests_left"] == 4 - i
    with pytest.raises(A.EvolveError) as e:
        C.register_formal_test(env.con, ids[5], p)
    assert e.value.code == "ALPHA_EXHAUSTED"
    with pytest.raises(A.EvolveError) as e:
        C.register_formal_test(env.con, ids[0], p)
    assert e.value.code == "ALREADY_REGISTERED"
    with pytest.raises(A.EvolveError) as e:
        C.consume_formal_test(env.con, ids[5], p)
    assert e.value.code == "NOT_REGISTERED"
    C.consume_formal_test(env.con, ids[0], p)
    C.consume_formal_test(env.con, ids[0], p)                             # 第二次是空操作
    a = C.alpha_summary(env.con, SCOPE, p)
    assert a["ALPHA_EXPANSION"]["formal_consumed"] == 1 and a["ALPHA_EXPANSION"]["alpha_consumed"] == p.alpha
    assert a["ALPHA_EXPANSION"]["alpha_remaining"] == 0 and a["PRUNING_DEFENSIVE"]["formal_tests_left"] == p.max_formal_tests


# ---------------------------------------------------------------- 队列
def test_score_formula():
    cfg, p = ecfg(), TR.cfg().power
    eff = {"mean_diff_per_day": 0.1}
    full = {"evidence_days": 20, "counter_share": 0.0, "bucket_share": 0.3}
    assert Q.score(None, full, "ALPHA_EXPANSION", cfg, p) is None
    assert Q.score(eff, full, "ALPHA_EXPANSION", cfg, p) == pytest.approx(0.1)
    assert Q.score(eff, {**full, "counter_share": 0.5}, "ALPHA_EXPANSION", cfg, p) == pytest.approx(0.05)
    assert Q.score(eff, {**full, "evidence_days": 5}, "ALPHA_EXPANSION", cfg, p) == pytest.approx(0.025)
    assert Q.score(eff, {**full, "counter_share": None}, "ALPHA_EXPANSION", cfg, p) == pytest.approx(0.05)
    assert Q.score(eff, {**full, "bucket_share": 0.9}, "ALPHA_EXPANSION", cfg, p) == pytest.approx(0.05)
    assert Q.score({"mean_diff_per_day": -0.2}, full, "ALPHA_EXPANSION", cfg, p) == 0
    # 剪枝防守：效应项是高出非劣效界的部分
    assert Q.score({"mean_diff_per_day": -0.01}, full, "PRUNING_DEFENSIVE", cfg, p) == pytest.approx(p.margin_r_per_day - 0.01)


def queued(env, rc, base, to=2, track="ALPHA_EXPANSION", now=None):
    c, cand = proposal(env, rc, base, to=to, eval_track=track)
    cand = C.validate(env.settings, env.con, {**cand, "base_release": Q.base_release_of(base["baseline_id"])}, c,
                      registry=TR.REG, cfg=ecfg())
    meta = {"source": "t", "fixture": False, "counter_share": c["counter_share"], "bucket_share": 0.3,
            "mechanism_key": C.mechanism_key(c)}
    C.save(env.settings, env.con, cand, meta, out_dir=env.tmp / "c", now=now)
    assert Q.enqueue(env.con, cand, meta, now=now) is True
    return cand["candidate_id"]


def test_queue_admission_ttl_and_base_replacement(env):
    base, rc = reviewed(env)
    cfg, p = ecfg(), rc.power
    current = Q.base_release_of(base["baseline_id"])
    a, b = queued(env, rc, base, 2, now=1000), queued(env, rc, base, 3, now=1000 + 100 * 86400)
    c, cand = proposal(env, rc, base, to=4)
    assert Q.enqueue(env.con, {**cand, "category": "data_engine"}, {}) is False
    assert Q.enqueue(env.con, cand, {"fixture": True}) is False            # 合成数据得到的候选不进队列
    r = Q.rank(env.con, SCOPE, cfg, p)
    assert [x["candidate_id"] for x in r] == sorted([a, b]) and all(x["score"] is None for x in r)
    assert Q.supersede(env.con, SCOPE, current, cfg, now=1000 + 110 * 86400) == []
    out = Q.supersede(env.con, SCOPE, current, cfg, now=1000 + 121 * 86400)
    assert out == [{"candidate_id": a, "reason": "QUEUE_TTL_EXPIRED"}] and C.get(env.con, a)["status"] == "SUPERSEDED"
    out = Q.supersede(env.con, SCOPE, "baseline:new", cfg, now=1000 + 121 * 86400)
    assert out == [{"candidate_id": b, "reason": "BASE_RELEASE_REPLACED"}] and Q.rank(env.con, SCOPE, cfg, p) == []
    led = [(x["event"], x["to_status"], json.loads(x["detail_json"]).get("reason")) for x in env.con.execute(
        "SELECT event, to_status, detail_json FROM candidate_ledger WHERE candidate_id=? ORDER BY entry_id", (b,))]
    assert led == [("CREATED", "PROPOSED", None), ("QUEUED", None, None),
                   ("STATUS_CHANGED", "SUPERSEDED", "BASE_RELEASE_REPLACED")]
    # 基准换了之后可以对着新基准重新提出；只是过期的不行
    cl, again = proposal(env, rc, base, to=3)
    again = C.validate(env.settings, env.con, {**again, "base_release": "baseline:new"}, cl, registry=TR.REG, cfg=cfg)
    s = C.save(env.settings, env.con, again, {"source": "t", "fixture": False}, out_dir=env.tmp / "c")
    assert s["action"] == "REPROPOSED" and Q.enqueue(env.con, again, {"fixture": False}) is True
    assert [x["candidate_id"] for x in Q.rank(env.con, SCOPE, cfg, p)] == [b]
    cl, old = proposal(env, rc, base, to=2)
    old = C.validate(env.settings, env.con, {**old, "base_release": current}, cl, registry=TR.REG, cfg=cfg)
    assert C.save(env.settings, env.con, old, {"source": "t"}, out_dir=env.tmp / "c")["action"] == "KEPT_SUPERSEDED"


def test_estimate_pairs_candidate_with_baseline(env):
    base, rc = reviewed(env, fwd=DAYS[3])
    cid = queued(env, rc, base, 2)
    kw = dict(research_cfg=rc, risk_cfg=TR.configured_risk(), engine_cfg=ER.ENG)
    e = Q.estimate(env.settings, env.con, env.scope, TR.REG, base, cid, **kw)
    assert e["run_id"] == f"cand:{base['baseline_id']}:{cid}" and not e["reused"]
    assert set(e["segments"]) == {"development", "forward_validation", "total"}
    t = e["segments"]["total"]
    assert t["days"] == 5 and t["base_trades"] == 10 and t["unit"] == "R"
    assert t["mean_diff_per_day"] == pytest.approx(t["candidate_net_per_day"] - t["base_net_per_day"])
    assert e["mean_diff_per_day"] == t["mean_diff_per_day"] and t["mean_diff_per_day"] != 0     # 止损放远，结果确实变了
    row = env.con.execute("SELECT role, status, diff_json FROM research_runs WHERE run_id=?", (e["run_id"],)).fetchone()
    assert (row["role"], row["status"], json.loads(row["diff_json"])) == ("CANDIDATE", "DONE", {KEY: 2})
    r = Q.rank(env.con, SCOPE, ecfg(), rc.power)
    assert r[0]["candidate_id"] == cid and r[0]["rank"] == 1 and r[0]["score"] is not None
    assert Q.estimate(env.settings, env.con, env.scope, TR.REG, base, cid, **kw)["reused"]
    n = env.con.execute("SELECT COUNT(*) FROM candidate_ledger WHERE candidate_id=? AND event='DEV_ATTEMPT'", (cid,))
    assert n.fetchone()[0] == 1                                            # 复用已跑过的回放不再计一次尝试
    assert C.alpha_summary(env.con, SCOPE, rc.power)["ALPHA_EXPANSION"]["dev_attempts"] == 1
    with pytest.raises(A.EvolveError) as x:
        Q.estimate(env.settings, env.con, env.scope, TR.REG, base, "cand_nope", **kw)
    assert x.value.code == "NOT_QUEUED"
    out = EV.estimate_queue(env.settings, env.con, env.scope, TR.REG, cfg=ecfg(), **kw)
    assert out["estimated"] == [] and out["queue"][0]["candidate_id"] == cid


def test_pre_eval_shadow_reports_runnability_only(env):
    base, rc = reviewed(env)
    cid = queued(env, rc, base, 2)
    kw = dict(cfg=ecfg(), risk_cfg=ER.RISK, engine_cfg=ER.ENG)
    ranked = Q.rank(env.con, SCOPE, ecfg(), rc.power)
    r = Q.pre_eval_shadow(env.settings, env.con, env.scope, TR.REG, base, ranked, **kw)
    assert r["status"] == "NO_ELIGIBLE_DAYS" and r["days"] == []
    assert env.con.execute("SELECT COUNT(*) FROM pre_eval_shadow_days").fetchone()[0] == 0
    later = TR.days_from(date(2025, 12, 1), 2)
    rows = []
    for d in later:
        rows += ER.scripted_day(d)
    ER.load(env, rows, name="later.csv")
    assert Q.shadow_days(env.con, env.scope, base) == [d.isoformat() for d in later]
    r = Q.pre_eval_shadow(env.settings, env.con, env.scope, TR.REG, base, ranked, **kw)
    x = r["results"][0]
    assert r["status"] == "DONE" and x["candidate_id"] == cid and x["ok"] is True and x["error"] is None
    assert x["new_days"] == [d.isoformat() for d in later]
    assert all(v == {"decisions": 78, "data_errors": 0, "engine_flags": 0} for v in x["per_day"].values())
    assert "pnl" not in json.dumps(r) and "net_usd" not in json.dumps(r)   # 预演不报告盈亏
    for t in ("scan_events", "paper_trades", "paper_orders", "engine_runs"):
        assert env.con.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id LIKE 'shadow:%'").fetchone()[0] == 0
    saved = env.con.execute("SELECT trading_day, result_json FROM pre_eval_shadow_days ORDER BY 1").fetchall()
    assert [s["trading_day"] for s in saved] == [d.isoformat() for d in later]
    again = Q.pre_eval_shadow(env.settings, env.con, env.scope, TR.REG, base, ranked, **kw)
    assert again["results"][0]["new_days"] == [] and again["results"][0]["ok"] is None      # 看过的日期不重复看


# ---------------------------------------------------------------- CLI 与边界
def run_cli(env, capsys, *argv):
    code = main(list(argv), settings=env.settings, registry=TR.REG)
    return code, capsys.readouterr().out


def test_cli_evolve(env, capsys, tmp_path):
    code, out = run_cli(env, capsys, "evolve", "--json")
    assert code == 1 and json.loads(out)["error"] == "NO_BASELINE"
    code, out = run_cli(env, capsys, "evolve", "--estimate", "--ledger")
    assert code == 2 and "一次只能用一个" in out
    base, rc = reviewed(env)
    env.con.execute("DELETE FROM session_reviews")
    env.con.commit()
    code, out = run_cli(env, capsys, "evolve", "--json")
    assert code == 1 and json.loads(out)["error"] == "NO_SESSIONS"
    code, out = run_cli(env, capsys, "evolve", "--ledger")
    assert code == 0 and "队列为空" in out and "ALPHA_EXPANSION" in out
    code, out = run_cli(env, capsys, "evolve", "--pre-eval-shadow", "--json")
    assert code == 0 and json.loads(out)["status"] == "NO_ELIGIBLE_DAYS"
    bad = tmp_path / "p.json"
    bad.write_text("[]", encoding="utf-8")
    code, out = run_cli(env, capsys, "evolve", "--import-proposals", str(bad), "--json")
    assert code == 1 and json.loads(out)["error"] == "PROPOSALS_UNREADABLE"


def test_evolve_package_boundaries():
    """不读保留集和库外行情，不调用模型，不碰网络，不含信号或成交逻辑。"""
    src = pathlib.Path(A.__file__).parent
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(src.glob("*.py")))
    for banned in ("withheld", "holdout", "anthropic", "import requests", "urllib", "socket", "subprocess", "import fills",
                   "INSERT INTO paper_", "INSERT INTO bars", "release_db", "open_release"):
        assert banned not in text, banned
    assert "risk.yaml" not in text and "accounts.yaml" not in text       # 候选碰不到风险上限
    keys = C.evolvable_keys(TR.REG.get("pa_brooks"))
    assert keys and not any("max_stops" in k or k.startswith(("risk", "account")) for k in keys)
