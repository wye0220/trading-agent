"""阶段 8：独立评测。门槛自测用合成序列；流程用标成"真实"的合成行情走完整的登记、解封、回放。"""
import copy
import dataclasses
import json
import sqlite3

import pytest
import yaml

import helpers as H
import test_engine_runner as ER
import test_research as TR
from conftest import Env
from trading_agent import sessions
from trading_agent.cli import main
from trading_agent.config import ConfigError
from trading_agent.evaluation import gates as G
from trading_agent.evaluation import guard as GD
from trading_agent.evaluation import run as EV
from trading_agent.evaluation import stats as ST
from trading_agent.evaluation import windows as W
from trading_agent.evaluation.config import load_eval, parse
from trading_agent.evolve import candidates as C
from trading_agent.memory import store as MS
from trading_agent.research import baseline as BL

RAW = yaml.safe_load((H.TEST_PROJECT / "config" / "evaluation.yaml").read_text(encoding="utf-8"))
KEY = "params.entry.stop_offset_ticks"
RISK = TR.configured_risk()
OK_SYN = lambda settings, cfg: {"ok": True, "files": [], "summary": "合成回归（测试替身）通过"}      # noqa: E731
BAD_SYN = lambda settings, cfg: {"ok": False, "files": [], "summary": "1 failed"}                  # noqa: E731


def ecfg(**over):
    raw = copy.deepcopy(RAW)
    raw["warmup_trading_days"] = 1
    raw["bootstrap"].update(block_length_days=1, resamples=200, min_tail_count=1, max_resamples=800)
    raw["isolation"]["author_users"] = []
    for path, v in over.items():
        node = raw
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = v
    return parse(raw)


CFG = ecfg()
POWER = TR.cfg(**{"power.min_trades_per_arm": 20}).power


def arms(base, cand, bt=None, ct=None):
    n = len(base)
    return {"days": [f"d{i}" for i in range(n)], "base_net": list(base), "cand_net": list(cand),
            "base_trades": bt or [1] * n, "cand_trades": ct or [1] * n}


# ---------------------------------------------------------------- 配置与统计
def test_config_validation():
    assert load_eval(H.TEST_PROJECT).digest == parse(RAW).digest and CFG.digest != parse(RAW).digest
    for path, v in (("bootstrap.resamples", 10), ("screen.reject_mean_diff_r_per_day", 0.1),
                    ("labeling.min_recognition_rate", 1.5), ("visible_summary.screen", ["status", "mean_diff"]),
                    ("visible_summary.confirm", ["reasons"]), ("synthetic_regression", ["../etc/passwd"]),
                    ("synthetic_regression", []), ("bootstrap.max_resamples", 100)):
        with pytest.raises(ConfigError):
            ecfg(**{path: v})
    assert "author_users" not in json.dumps(CFG.body()) and "sealed" not in json.dumps(CFG.body())


def test_block_bound_is_reproducible_and_refuses_when_unreliable():
    xs = [((i * 37) % 23 - 9) / 40 for i in range(40)]
    kw = dict(alpha=0.01, seed=7, block=5, resamples=2000, min_tail=20, max_resamples=8000)
    a, b = ST.block_bound(xs, **kw), ST.block_bound(xs, **kw)
    assert a == b and a["bound"] < a["mean"] and a["resamples"] == 2000
    assert ST.block_bound(xs, **{**kw, "seed": 8})["bound"] != a["bound"]
    assert ST.block_bound(xs, **{**kw, "side": "upper"})["bound"] > a["mean"]
    assert ST.block_bound(xs[:9], **kw)["reason"] == "TOO_FEW_DAYS_FOR_BLOCKS"
    grown = ST.block_bound(xs, **{**kw, "resamples": 500})
    assert grown["resamples"] == 2000 and grown["bound"] is not None           # 尾部太稀：加倍重采样
    sparse = ST.block_bound(xs, **{**kw, "resamples": 500, "max_resamples": 1000})
    assert sparse["bound"] is None and sparse["reason"] == "TAIL_TOO_SPARSE"
    assert ST.max_drawdown([1, -2, -1, 3]) == 3 and ST.max_drawdown([]) == 0
    with pytest.raises(ValueError):
        ST.paired([1], [1, 2])


def test_delta_trades_is_auxiliary():
    base = [{"setup_id": f"s{i}", "net_usd": -100 if i % 2 else 50} for i in range(30)]
    cand = [t for t in base if int(t["setup_id"][1:]) % 2 == 0] + [{"setup_id": "new", "net_usd": 10}]
    d = ST.delta_trades(base, cand, 500.0, alpha=0.01, seed=1, resamples=500)
    assert d["blocked_trades"] == 15 and d["candidate_only_trades"] == 1 and d["blocked_mean_r"] == -0.2
    assert d["upper_bound"] == pytest.approx(-0.2)
    assert ST.delta_trades(base[:4], [], 500.0, alpha=0.01, seed=1, resamples=500)["upper_bound"] is None


# ---------------------------------------------------------------- 门槛自测（第 8 节）
def test_good_twenty_days_cannot_promote():
    r = G.screen(arms([0.0] * 20, [1.0] * 20), CFG, required_days=20)
    assert r["status"] == "SCREEN_CONTINUE" and "bound" not in r and "gates" not in r
    assert "PASS" not in json.dumps(r) and "p_value" not in json.dumps(r)


def test_clearly_worse_twenty_days_stops():
    assert G.screen(arms([0.1] * 20, [-0.1] * 20), CFG, required_days=20)["reasons"] == ["PAIRED_NET_R_DEGRADED"]
    dd = G.screen(arms([0.0] * 20, [2.0] * 10 + [-0.5] * 10), CFG, required_days=20)
    assert dd["status"] == "SCREEN_REJECTED" and dd["reasons"] == ["DRAWDOWN_WORSENED"]
    short = G.screen(arms([0.0] * 12, [1.0] * 12), CFG, required_days=20)
    assert short["status"] == "EVIDENCE_INSUFFICIENT" and short["reasons"] == ["VALID_DAYS_SHORT:12/20"]
    err = G.screen(arms([0.0] * 20, [1.0] * 20), CFG, required_days=20, errors=["ENGINE_FLAG:UNPRICEABLE"])
    assert err["status"] == "SCREEN_REJECTED"                                   # 引擎或风控错误零容忍


def kw40(**over):
    return {"required_days": 40, "stress_r_per_trade": 0.05, **over}


def test_track_b_gates():
    base = [0.1, -0.1] * 20
    good = [b + 0.2 + (0.05 if i % 3 else -0.05) for i, b in enumerate(base)]
    r = G.confirm(arms(base, good), G.TRACK_B, POWER, CFG, **kw40())
    assert r["status"] == "PASS_FOR_SHADOW" and r["reasons"] == [] and all(r["gates_passed"].values())
    assert r["bound"]["bound"] > 0 and r["bound"]["alpha"] == POWER.alpha == 0.01
    for g in r["gates"]:
        assert set(g) == {"gate", "passed", "value", "threshold", "denominator", "note"}
    small = G.confirm(arms(base, [b + 0.01 for b in base]), G.TRACK_B, POWER, CFG, **kw40())
    assert small["status"] == "FAIL" and "MEAN_PAIRED_UPLIFT" in small["reasons"]
    noisy = [b + (2.0 if i % 10 == 0 else -0.1) for i, b in enumerate(base)]
    n = G.confirm(arms(base, noisy), G.TRACK_B, POWER, CFG, **kw40())
    assert n["detail"]["mean_diff"] >= POWER.delta_r_per_day and "LOWER_BOUND_ABOVE_ZERO" in n["reasons"]
    dd = [b + 0.2 for b in base[:20]] + [b - 0.2 + 0.4 * (i >= 12) for i, b in enumerate(base[20:])]
    assert "DRAWDOWN_INCREASE_R" in G.confirm(arms(base, dd), G.TRACK_B, POWER, CFG, **kw40())["reasons"]
    reg = G.confirm(arms(base, good), G.TRACK_B, POWER, CFG, **kw40(regression_ok=False))
    assert reg["status"] == "FAIL" and reg["reasons"] == ["REGRESSION_FAILED"]   # 回归零容忍
    losing = G.confirm(arms([-0.5] * 40, [-0.2 + (0.01 if i % 2 else -0.01) for i in range(40)]), G.TRACK_B, POWER, CFG, **kw40())
    assert losing["status"] == "FAIL" and "STRESS_NET_POSITIVE" in losing["reasons"]
    with pytest.raises(ValueError):
        G.confirm(arms(base, good), "OTHER", POWER, CFG, **kw40())


def test_low_frequency_and_unestimable_are_insufficient_not_fail():
    base = [0.1, -0.1] * 20
    good = [b + 0.3 for b in base]
    few = G.confirm(arms(base, good, bt=[1] + [0] * 39, ct=[1] * 40), G.TRACK_B, POWER, CFG, **kw40())
    assert few["status"] == "EVIDENCE_INSUFFICIENT" and few["reasons"] == ["TRADES_SHORT:1/20"]
    short = G.confirm(arms(base[:30], good[:30]), G.TRACK_B, POWER, CFG, **kw40())
    assert short["status"] == "EVIDENCE_INSUFFICIENT" and "VALID_DAYS_SHORT:30/40" in short["reasons"]
    wide = ecfg(**{"bootstrap.block_length_days": 30})
    un = G.confirm(arms(base, good), G.TRACK_B, POWER, wide, **kw40())
    assert un["status"] == "EVIDENCE_INSUFFICIENT" and un["reasons"] == ["BOUND_NOT_ESTIMABLE:TOO_FEW_DAYS_FOR_BLOCKS"]
    assert un["bound"]["bound"] is None


def test_track_a_gates():
    base = [0.5, -0.4, 0.4, -0.2] * 10
    pruned = [0.5, -0.1, 0.4, -0.1] * 10
    r = G.confirm(arms(base, pruned), G.TRACK_A, POWER, CFG, **kw40())
    assert r["status"] == "PASS_FOR_SHADOW" and r["bound"]["bound"] > -POWER.margin_r_per_day
    same_dd = G.confirm(arms(base, [b + 0.001 for b in base]), G.TRACK_A, POWER, CFG, **kw40())
    assert same_dd["status"] == "FAIL" and same_dd["reasons"] == ["DRAWDOWN_IMPROVEMENT_PCT"]
    worse = G.confirm(arms(base, [b - 0.2 for b in base]), G.TRACK_A, POWER, CFG, **kw40())
    assert "NON_INFERIORITY_LOWER_BOUND" in worse["reasons"]


def test_labeling_gates():
    def s(e, p, tot=2, ok=2):
        return {"expected": e, "predicted": p, "citations_total": tot, "citations_correct": ok}
    good = [s(True, True)] * 18 + [s(True, False)] * 2 + [s(False, False)] * 19 + [s(False, True)]
    r = G.labeling(good, CFG, regression_ok=True, logic_unchanged=True)
    assert r["status"] == "PASSED" and r["counts"] == {"tp": 18, "fn": 2, "fp": 1, "tn": 19, "citations_total": 80,
                                                       "citations_correct": 80}
    assert G.labeling(good[:10], CFG, regression_ok=True, logic_unchanged=True)["status"] == "EVIDENCE_INSUFFICIENT"
    bad = [s(True, False)] * 10 + good[10:]
    assert G.labeling(bad, CFG, regression_ok=True, logic_unchanged=True)["reasons"] == ["RECOGNITION_RATE"]
    cite = [s(True, True, 2, 1)] * 18 + good[18:]
    assert G.labeling(cite, CFG, regression_ok=True, logic_unchanged=True)["reasons"] == ["CITATION_ACCURACY"]
    assert G.labeling(good, CFG, regression_ok=False, logic_unchanged=True)["status"] == "FAILED"
    assert G.labeling(good, CFG, regression_ok=True, logic_unchanged=False)["status"] == "FAILED"


# ---------------------------------------------------------------- 流程：标成"真实"的合成行情
def real_day(day):
    rows = ER.scripted_day(day, source="databento")
    for r in rows:
        r.update(fixture="0" if isinstance(r["fixture"], str) else False, contract_id="ESZ2025")
    return rows


def eval_env(tmp_path, n_dev=4, n_held=13):
    d = TR.days_from(TR.START, n_dev + n_held)
    e = Env(tmp_path, dev_visible_through=d[n_dev - 1])
    e.settings = dataclasses.replace(e.settings, holdout_root=tmp_path / "holdout")
    rows = []
    for x in d:
        rows += real_day(x)
    H.write_csv(e.root / "real.csv", rows)
    e.ingest()
    rc = TR.cfg(**{"splits.capsule_valid_days": 2, "splits.forward_validation_start": d[2].isoformat(),
                   "splits.warmup_trading_days": 0, "power.min_trades_per_arm": 2})
    base = BL.freeze(e.settings, e.con, e.scope, TR.REG.get("pa_brooks"), rc, risk_cfg=RISK, engine_cfg=ER.ENG,
                     allow_real_data=True)
    assert base["status"] == "FROZEN" and e.count("trading_day > ?", d[n_dev - 1].isoformat()) == 0
    return e, d, rc, base


def add_candidate(e, base, to=2, track="ALPHA_EXPANSION", cid=None):
    cid = cid or f"cand_{to:016x}"
    cand = {"candidate_id": cid, "status": "PROPOSED", "scope": str(e.scope), "category": "strategy_rule",
            "eval_track": track, "base_release": f"baseline:{base['baseline_id']}", "evidence_event_ids": ["e1"],
            "evidence_dates": ["2025-09-15"], "observed_problem": "x", "hypothesis": "y",
            "exact_diff": [{"target": "strategy_config", "key": KEY, "from": 1, "to": to}],
            "primary_metric": "paired_net_r_per_day", "rollback_trigger": "z"}
    with e.con:
        e.con.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,1,0,0)",
                      (cid, str(e.scope), "strategy_rule", track, "PROPOSED", cand["base_release"],
                       json.dumps({"candidate": cand, "meta": {}}), None))
    return cid


def stage(fn, e, cid, rc, cfg=CFG, synthetic=OK_SYN, **kw):
    return fn(e.settings, e.con, e.scope, TR.REG, cid, cfg=cfg, research_cfg=rc, risk_cfg=RISK, engine_cfg=ER.ENG,
              synthetic=synthetic, out_dir=e.tmp / "evals", **kw)


def refused(fn, *a, **kw):
    with pytest.raises(EV.EvalError) as x:
        fn(*a, **kw)
    return x.value.reason


def ledger(e, cid, purpose):
    return [r[0] for r in e.con.execute("SELECT DISTINCT event FROM window_ledger WHERE candidate_id=? AND purpose=?"
                                        " ORDER BY entry_id", (cid, purpose))]


ELIG = {"ALPHA_EXPANSION": "ELIGIBLE", "PRUNING_DEFENSIVE": "ELIGIBLE"}


def test_full_protocol_flow(tmp_path):
    e, d, rc, base = eval_env(tmp_path)
    c1, c2, c3 = (add_candidate(e, base, to) for to in (2, 3, 4))
    me = GD.current_user()

    # 预登记
    assert refused(EV.screen, e.settings, e.con, e.scope, TR.REG, c1, cfg=CFG, research_cfg=rc) == "NOT_REGISTERED"
    r1 = EV.register(e.settings, e.con, e.scope, [c1], cfg=CFG, research_cfg=rc, eligibility={})
    assert r1["screen_window"]["capsules"] == [1] and r1["confirm_window"]["capsules"] == [2, 3]
    assert r1["confirm_window"]["valid_days"] == 4 and r1["registrations"][0]["power_eligibility"] == "UNKNOWN"
    assert refused(EV.register, e.settings, e.con, e.scope, [c1], cfg=CFG, research_cfg=rc) == "ALREADY_REGISTERED"
    assert refused(EV.register, e.settings, e.con, e.scope, ["cand_missing"], cfg=CFG, research_cfg=rc) == "NO_CANDIDATE"
    proto = EV.registration(e.con, c1)["protocol"]
    assert proto["alpha_per_test"] == 0.01 and proto["one_sided"] and proto["costs"]["research_risk_unit_usd"] == "500"
    assert proto["windows"]["confirm"]["start_day"] == d[6].isoformat() and proto["thresholds"]["confirm_required_days"] == 4
    for sql in ("UPDATE evaluation_registrations SET candidate_sha256='x'", "DELETE FROM evaluation_registrations",
                "UPDATE evaluation_protocols SET body_json='{}'", "DELETE FROM window_ledger",
                "UPDATE window_ledger SET event='RETURNED'"):
        with pytest.raises(sqlite3.DatabaseError):
            e.con.execute(sql)
        e.con.rollback()

    # 守卫：作者身份、没有清单、清单不符
    author = ecfg(**{"isolation.author_users": [me]})
    assert author.body() == CFG.body()
    assert refused(stage, EV.screen, e, c1, rc, cfg=author) == "AUTHOR_IDENTITY"
    with pytest.raises(GD.GuardError):
        GD.accept(e.settings, author)
    assert refused(stage, EV.screen, e, c1, rc) == "MANIFEST_MISSING"
    GD.accept(e.settings, CFG)
    man = GD.manifest_path(e.settings)
    good = man.read_text()
    man.write_text(good.replace(json.loads(good)["evaluator_sha256"], "0" * 64))
    assert refused(stage, EV.screen, e, c1, rc) == "DIGEST_MISMATCH"
    man.write_text(good)
    assert ledger(e, c1, "SCREEN") == ["ASSIGNED"] and not (e.settings.holdout_root / "evaluation").exists()
    assert refused(stage, EV.screen, e, c1, rc, cfg=ecfg(**{"screen.reject_mean_diff_r_per_day": -0.5})) == "PROTOCOL_CHANGED"
    assert refused(stage, EV.confirm, e, c1, rc) == "SCREEN_REQUIRED"

    # 阶段 A：盲筛
    s1 = stage(EV.screen, e, c1, rc)
    assert s1["status"] == "SCREEN_CONTINUE" and s1["valid_days"] == 2 and s1["reasons"] == []
    assert set(s1) == {"evaluation_id", "stage", "candidate_id", "status", "reasons", "valid_days", "summary_report"}
    row = e.con.execute("SELECT status, result_json FROM evaluation_runs WHERE evaluation_id=?", (s1["evaluation_id"],)).fetchone()
    assert json.loads(row["result_json"]) == {"status": "SCREEN_CONTINUE", "reasons": [], "valid_days": 2}   # 没有盈亏数字
    summary = open(s1["summary_report"], encoding="utf-8").read()
    assert "SCREEN_CONTINUE" in summary and "净 R" not in summary and "PASS" not in summary
    full = json.loads((e.settings.holdout_root / "evaluation" / "reports" / f"{s1['evaluation_id']}.json").read_text())
    assert len(full["daily"]) == 2 and full["result"]["detail"]["base_trades"] == 4 and "bound" not in full["result"]
    assert full["result"]["detail"]["mean_diff"] < 0 and full["warmup_start"] == d[3].isoformat()
    assert full["breakdown"]["合约"]["ESZ2025"]["base_trades"] == 4 and set(full["breakdown"]) == {"入场时段", "合约", "当日波动"}
    assert ledger(e, c1, "SCREEN") == ["ASSIGNED", "UNSEALED"] and ledger(e, c1, "CONFIRM") == ["ASSIGNED"]
    assert C.get(e.con, c1)["status"] == "SCREENED"
    assert refused(stage, EV.screen, e, c1, rc) == "ALREADY_VIEWED"             # 每段只看一次
    # 运行库里没有截止日之后的行情，也没有评测的逐笔
    assert e.count("trading_day > ?", d[3].isoformat()) == 0
    assert e.con.execute("SELECT COUNT(*) FROM paper_trades WHERE run_id LIKE 'eval:%'").fetchone()[0] == 0

    # 阶段 B：功效资格不足 → 不解封、不扣 α、确认窗退回
    k1 = stage(EV.confirm, e, c1, rc)
    assert k1["status"] == "EVIDENCE_INSUFFICIENT" and k1["reasons"] == ["POWER_ELIGIBILITY:UNKNOWN"]
    assert k1["confirm_window_returned"] and ledger(e, c1, "CONFIRM") == ["ASSIGNED", "RETURNED"]
    assert C.alpha_summary(e.con, str(e.scope), rc.power)["ALPHA_EXPANSION"]["formal_registered"] == 0
    assert C.get(e.con, c1)["status"] == "RESEARCH_OBSERVATION"
    assert refused(stage, EV.confirm, e, c1, rc) == "ALREADY_VIEWED"

    # 第二个候选：盲筛窗 1 已经披露，不再分配；退回的 2、3 可以用
    r2 = EV.register(e.settings, e.con, e.scope, [c2], cfg=CFG, research_cfg=rc, eligibility=ELIG)
    assert r2["screen_window"]["capsules"] == [2] and r2["confirm_window"]["capsules"] == [3, 4]
    body = json.loads(e.con.execute("SELECT body_json FROM candidates WHERE candidate_id=?", (c2,)).fetchone()[0])
    changed = copy.deepcopy(body)
    changed["candidate"]["exact_diff"][0]["to"] = 9
    with e.con:
        e.con.execute("UPDATE candidates SET body_json=? WHERE candidate_id=?", (json.dumps(changed), c2))
    assert refused(stage, EV.screen, e, c2, rc) == "CANDIDATE_CHANGED"          # 登记之后改了候选
    with e.con:
        e.con.execute("UPDATE candidates SET body_json=? WHERE candidate_id=?", (json.dumps(body), c2))
    assert stage(EV.screen, e, c2, rc)["status"] == "SCREEN_CONTINUE"
    k2 = stage(EV.confirm, e, c2, rc)
    assert k2["status"] == "FAIL" and k2["valid_days"] == 4 and "MEAN_PAIRED_UPLIFT" in k2["reasons"]
    assert k2["gates_passed"]["VALID_DAYS"] and k2["gates_passed"]["MIN_TRADES_PER_ARM"] and k2["gates_passed"]["REGRESSION_ZERO_FAILURES"]
    assert not k2["gates_passed"]["STRESS_NET_POSITIVE"] and k2["confirm_window_returned"] is False
    full = json.loads((e.settings.holdout_root / "evaluation" / "reports" / f"{k2['evaluation_id']}.json").read_text())
    assert [x["day"] for x in full["daily"]] == [x.isoformat() for x in d[8:12]]      # 只有确认窗的日子，不并入盲筛
    assert full["result"]["bound"]["alpha"] == 0.01 and full["result"]["bound"]["bound"] is not None
    for g in full["result"]["gates"]:
        assert "denominator" in g and "threshold" in g
    a = C.alpha_summary(e.con, str(e.scope), rc.power)["ALPHA_EXPANSION"]
    assert (a["formal_registered"], a["formal_consumed"], a["formal_tests_left"]) == (1, 1, 4)
    assert C.get(e.con, c2)["status"] == "FAILED" and ledger(e, c2, "CONFIRM") == ["ASSIGNED", "UNSEALED"]
    assert refused(stage, EV.confirm, e, c2, rc) == "ALREADY_VIEWED"            # 正式确认不能重复查看
    assert refused(EV.register, e.settings, e.con, e.scope, [c2], cfg=CFG, research_cfg=rc) == "NOT_PROPOSED"

    # 改动后的候选是新候选：已披露的 1–4 都不能再用，剩下的不够
    assert refused(EV.register, e.settings, e.con, e.scope, [c3], cfg=CFG, research_cfg=rc, eligibility=ELIG) == "NO_WINDOW"
    st = EV.status(e.con, e.scope, rc)
    assert st["capsules"] == {"total": 6, "FREE": 2, "ASSIGNED": 0, "UNSEALED": 4}
    assert [(x["screen"], x["confirm"]) for x in st["registrations"]] == [
        ("SCREEN_CONTINUE", "EVIDENCE_INSUFFICIENT"), ("SCREEN_CONTINUE", "FAIL")]


def test_screen_rejection_returns_confirm_window_and_regression_blocks(tmp_path):
    e, d, rc, base = eval_env(tmp_path, n_held=12)
    GD.accept(e.settings, CFG)
    strict = ecfg(**{"screen.reject_mean_diff_r_per_day": -0.001})
    GD.accept(e.settings, strict)
    c1, c2, c3 = (add_candidate(e, base, to) for to in (2, 3, 4))
    # 同一次登记的两个候选共用窗口
    r = EV.register(e.settings, e.con, e.scope, [c1, c2], cfg=strict, research_cfg=rc, eligibility=ELIG)
    assert len(r["registrations"]) == 2 and r["confirm_window"]["capsules"] == [2, 3]
    assert refused(EV.register, e.settings, e.con, e.scope, [c3, c3], cfg=strict, research_cfg=rc) == "NO_CANDIDATE"
    s1 = stage(EV.screen, e, c1, rc, cfg=strict)
    assert s1["status"] == "SCREEN_REJECTED" and s1["reasons"] == ["PAIRED_NET_R_DEGRADED"]
    assert s1["confirm_window_returned"] and ledger(e, c1, "CONFIRM") == ["ASSIGNED", "RETURNED"]
    assert refused(stage, EV.confirm, e, c1, rc, cfg=strict) == "SCREEN_REQUIRED" and C.get(e.con, c1)["status"] == "FAILED"
    caps = W.capsules(e.con, base["registry_id"])
    assert W.state(e.con, "ES", caps) == {1: "UNSEALED", 2: "ASSIGNED", 3: "ASSIGNED", 4: "FREE", 5: "FREE", 6: "FREE"}
    # 回归失败：直接淘汰，窗口不解封
    s2 = stage(EV.screen, e, c2, rc, cfg=strict, synthetic=BAD_SYN)
    assert s2["status"] == "SCREEN_REJECTED" and s2["reasons"] == ["REGRESSION_FAILED"] and s2["valid_days"] == 0
    assert ledger(e, c2, "SCREEN") == ["ASSIGNED"] and ledger(e, c2, "CONFIRM") == ["ASSIGNED", "RETURNED"]
    # 两个候选都止步后，确认窗才回到可用池；它们的盲筛窗已披露，不再可用
    assert W.state(e.con, "ES", caps) == {1: "UNSEALED", 2: "FREE", 3: "FREE", 4: "FREE", 5: "FREE", 6: "FREE"}
    r3 = EV.register(e.settings, e.con, e.scope, [c3], cfg=strict, research_cfg=rc, eligibility=ELIG)
    assert r3["screen_window"]["capsules"] == [2] and r3["confirm_window"]["capsules"] == [3, 4]
    # 窗口内容与登记不符：样本不足，不给结论
    with e.con:
        e.con.execute("UPDATE data_segments SET content_sha256='tampered' WHERE registry_id=? AND kind='CAPSULE' AND seq=2",
                      (base["registry_id"],))
    s3 = stage(EV.screen, e, c3, rc, cfg=strict)
    assert s3["status"] == "EVIDENCE_INSUFFICIENT" and s3["reasons"] == ["WINDOW_CONTENT_MISMATCH:capsule_2"]


def test_alpha_exhausted_blocks_confirm_before_unseal(tmp_path):
    e, d, rc, base = eval_env(tmp_path, n_held=6)
    GD.accept(e.settings, CFG)
    cid = add_candidate(e, base)
    rc1 = TR.cfg(**{"splits.capsule_valid_days": 2, "splits.forward_validation_start": d[2].isoformat(),
                    "splits.warmup_trading_days": 0, "power.min_trades_per_arm": 2, "power.max_formal_tests": 1})
    other = add_candidate(e, base, 5)
    C.register_formal_test(e.con, other, rc1.power)                             # 家族里唯一的一次已经用掉
    EV.register(e.settings, e.con, e.scope, [cid], cfg=CFG, research_cfg=rc1, eligibility=ELIG)
    assert stage(EV.screen, e, cid, rc1)["status"] == "SCREEN_CONTINUE"
    assert refused(stage, EV.confirm, e, cid, rc1) == "ALPHA_EXHAUSTED"
    assert ledger(e, cid, "CONFIRM") == ["ASSIGNED"]
    assert e.con.execute("SELECT COUNT(*) FROM evaluation_runs WHERE stage='CONFIRM'").fetchone()[0] == 0


# ---------------------------------------------------------------- 守卫与隔离检查
def test_isolation_check_reports_readable_paths(tmp_path, env):
    s = dataclasses.replace(env.settings, holdout_root=tmp_path / "holdout")
    me = GD.current_user()
    author = ecfg(**{"isolation.author_users": [me]})
    r = GD.isolation_check(s, author)
    assert r["result"] == "FAIL" and "HOLDOUT_ROOT_MISSING" in r["problems"] and set(r["paths"].values()) == {"BLOCKED"}
    (tmp_path / "holdout").mkdir()
    (tmp_path / "holdout" / "CANARY").write_text("secret-content")
    r = GD.isolation_check(s, author)
    assert r["result"] == "FAIL" and set(r["paths"].values()) == {"READABLE"} and "secret-content" not in json.dumps(r)
    assert {"PYTHON_OPEN_READABLE", "SHELL_CAT_READABLE", "SYMLINK_READABLE", "SEALED_DATA_OUTSIDE_HOLDOUT"} <= set(r["problems"])
    (tmp_path / "holdout").chmod(0o000)
    try:
        inside = ecfg(**{"isolation.author_users": [me], "isolation.sealed_data_root": str(tmp_path / "holdout" / "data")})
        r = GD.isolation_check(s, inside)
        assert r["result"] == "PASS" and set(r["paths"].values()) == {"BLOCKED"} and r["problems"] == []
    finally:
        (tmp_path / "holdout").chmod(0o700)
    assert GD.isolation_check(s, CFG)["result"] == "NOT_APPLICABLE"            # 不是作者身份跑的不算验收


def test_evaluator_digest_tracks_code_and_config():
    a = GD.evaluator_digest(H.PROJECT)
    assert a == GD.evaluator_digest(H.PROJECT) and len(a) == 64
    # 测试项目清掉了个人的封存路径，config/evaluation.yaml 不同，摘要也就不同：摘要跟踪配置
    assert GD.evaluator_digest(H.TEST_PROJECT) != a


def test_synthetic_regression_runs_listed_files(env):
    ok = EV.synthetic_regression(env.settings, ecfg(synthetic_regression=["tests/test_config.py"]))
    assert ok["ok"] and "passed" in ok["summary"]
    missing = EV.synthetic_regression(env.settings, ecfg(synthetic_regression=["tests/test_nope.py"]))
    assert not missing["ok"]
    assert set(RAW["synthetic_regression"]) >= {"tests/test_features.py", "tests/test_fills.py", "tests/test_risk.py"}


# ---------------------------------------------------------------- 标注类与 Memory
def test_labels_evaluation_unlocks_memory_item(env, tmp_path):
    import test_memory as TM
    cid = MS.propose(env.settings, env.con, TM.pattern(), origin="ev_1", reg=TM.REG, now=TM.NOW)["memory_candidate_id"]
    MS.confirm(env.settings, env.con, cid, reg=TM.REG, now=TM.NOW)

    def write(samples, who=cid):
        p = tmp_path / "labels.json"
        p.write_text(json.dumps({"candidate_id": who, "samples": samples}))
        return p

    def s(i, e, p, tot=1, ok=1):
        return {"sample_id": f"s{i}", "expected": e, "predicted": p, "citations_total": tot, "citations_correct": ok}
    few = [s(i, True, True) for i in range(5)]
    kw = dict(cfg=CFG, synthetic=OK_SYN)
    r = EV.labels(env.settings, env.con, cid, write(few), **kw)
    assert r["status"] == "EVIDENCE_INSUFFICIENT"
    with pytest.raises(MS.MemoryError_):
        MS.activate_evaluated(env.settings, env.con, cid, r["evaluation_id"], reg=TM.REG, now=TM.NOW)
    assert refused(EV.labels, env.settings, env.con, cid, write(few), **kw) == "ALREADY_VIEWED"
    assert refused(EV.labels, env.settings, env.con, cid, write(few, "memc_other"), **kw) == "LABELS_INVALID"
    assert refused(EV.labels, env.settings, env.con, cid, write([{"sample_id": "a", "expected": "批准"}]), **kw) == "LABELS_INVALID"
    assert refused(EV.labels, env.settings, env.con, "memc_none", write(few, "memc_none"), **kw) == "NO_CANDIDATE"
    good = [s(i, True, True) for i in range(20)] + [s(i + 20, False, False) for i in range(20)]
    bad = EV.labels(env.settings, env.con, cid, write(good), cfg=CFG, synthetic=BAD_SYN)
    assert bad["status"] == "FAILED" and "REGRESSION_ZERO_FAILURES" in bad["reasons"]
    ok = EV.labels(env.settings, env.con, cid, write(good + [s(99, False, False)]), **kw)
    assert ok["status"] == "PASSED"
    a = MS.activate_evaluated(env.settings, env.con, cid, ok["evaluation_id"], reg=TM.REG, now=TM.NOW)
    assert a["status"] == "CONFIRMED" and env.con.execute("SELECT confirmed_by FROM memory_items").fetchone()[0] == f"evaluation:{ok['evaluation_id']}"


# ---------------------------------------------------------------- CLI
def test_cli_evaluate(env, capsys, tmp_path):
    s = dataclasses.replace(env.settings, holdout_root=tmp_path / "holdout")

    def run(*argv):
        code = main(["evaluate", *argv, "--json"], settings=s)
        return code, json.loads(capsys.readouterr().out)
    code, out = run()
    assert code == 0 and out["registrations"] == [] and out["capsules"]["total"] == 0
    code, out = run("--incident-list")
    assert code == 0 and out == {"ok": True, "library": [], "drafts": []}
    code, out = run("--digests")
    assert code == 0 and out["manifest"] in ("MANIFEST_MISSING", "AUTHOR_IDENTITY") and len(out["evaluator_sha256"]) == 64
    code, out = run("--isolation-check")
    assert code == 1 and out["result"] in ("FAIL", "NOT_APPLICABLE")
    code, out = run("--register", "cand_x")
    assert code == 1 and out["error"] == "NO_BASELINE"
    code, out = run("--screen", "cand_x")
    assert code == 1 and out["error"] == "NOT_REGISTERED"
    code, out = run("--incident-confirm", "inc_x")
    assert code == 1 and out["error"] == "NO_DRAFT"
    assert main(["evaluate"], settings=s) == 0 and "胶囊 0 个" in capsys.readouterr().out


def test_evaluation_has_no_signal_fill_or_release_logic():
    import pathlib
    for pkg in ("evaluation", "incidents"):
        for f in pathlib.Path(H.PROJECT / "src" / "trading_agent" / pkg).glob("*.py"):
            text = f.read_text(encoding="utf-8")
            for banned in ("active_pointers", "open_release", "place_", "requests", "urllib", "socket", "anthropic",
                           "trigger_ticks <", "evaluate_fill", " exec(", " eval(", "importlib"):
                assert banned not in text, (f.name, banned)


def test_accepted_manifest_is_read_only_for_others(env, tmp_path):
    import dataclasses
    import stat
    s = dataclasses.replace(env.settings, holdout_root=tmp_path / "hold")
    GD.accept(s, ecfg(), user="evaluator")
    mode = stat.S_IMODE(GD.manifest_path(s).stat().st_mode)
    assert mode == 0o644
