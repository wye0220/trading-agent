"""阶段 9：影子、发布、冻结、回滚与发布后监控。全部在临时的运行库和发布库上演练（合成行情）。"""
import copy
import io
import json
import sqlite3
import time

import pytest
import yaml

import helpers as H
import test_engine_runner as ER
import test_research as TR
from trading_agent import db, scanner, sessions
from trading_agent.cli import main
from trading_agent.config import ConfigError, parse_scope
from trading_agent.evaluation.run import candidate_digest
from trading_agent.evolve import candidates as cands
from trading_agent.manual.common import paper_run_id
from trading_agent.release import freeze as RF
from trading_agent.release import manifest as RM
from trading_agent.release import monitor as MON
from trading_agent.release import pointer as P
from trading_agent.release import run as RR
from trading_agent.release import shadow as SH
from trading_agent.release.config import parse as rparse
from trading_agent.release.guard import Approval, ReleaseError, check_approval, cli_confirm, release_digest, verify_release_code
from trading_agent.research import baseline as BL

RAW = yaml.safe_load((H.TEST_PROJECT / "config" / "release.yaml").read_text(encoding="utf-8"))
KEY = "params.entry.stop_offset_ticks"


def rcfg(**over):
    raw = copy.deepcopy(RAW)
    raw["shadow"].update(min_valid_days=2, max_valid_days=4, min_fills_per_arm=2)
    raw["initial_baseline"]["min_paper_run_days"] = 1
    raw["monitor"].update(rolling_valid_days=2, notify_command=None)
    for path, v in over.items():
        node = raw
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = v
    return rparse(raw)


CFG = rcfg()
PASS = {k: (lambda: {"status": "PASS", "detail": "测试替身"}) for k in ("engineering", "vectors", "read_only")}
APP = Approval("tester", "TEST")
NOGUARD = lambda settings: None   # noqa: E731
DAYS = TR.days_from(TR.START, 14)
DEV, LIVE = DAYS[:3], DAYS[3:]
KW = dict(risk_cfg=ER.RISK, engine_cfg=ER.ENG)


def at(day, hh=16, mm=10):
    return H.et(day, hh, mm)


def times(day):
    """逐根推进脚本时段（信号来得及挂单），再在收盘后推进一次（截止强平和到期）。"""
    return [ER.bar_end(i, day) + 5 for i in range(-1, 14)] + [sessions.rth_bounds(day)[1] + 600]


def full_times(day):
    """整天逐根推进：纸面运行验收只数按时处理的交易日。"""
    return [s + 300 + 5 for s in sessions.expected_rth_slots(day)] + [sessions.rth_bounds(day)[1] + 600]


class Rel:
    def __init__(self, env):
        self.env = env
        TR.fixture_env(env, DEV)
        c = TR.cfg(**{"splits.forward_validation_start": DEV[2].isoformat(), "splits.warmup_trading_days": 0})
        self.base = BL.freeze(env.settings, env.con, env.scope, TR.REG.get("pa_brooks"), c, **KW)
        assert self.base["status"] == "FROZEN"
        self.rw = db.open_release(env.settings)
        self.loaded = set()

    @property
    def s(self):
        return self.env.settings

    @property
    def con(self):
        return self.env.con

    def ro(self):
        return P.open_readonly(self.s.release_db)

    def add_day(self, day):
        if day not in self.loaded:
            ER.load(self.env, ER.scripted_day(day), name=f"live_{day}.csv")
            self.loaded.add(day)

    def scan(self, t, **kw):
        ro = self.ro()
        try:
            return scanner.live_scan(self.s, self.con, self.env.scope, ER.PA, now_utc=t, release_con=ro,
                                     registry=TR.REG, **KW, **kw)
        finally:
            ro.close()

    def paper_day(self, day, shadow=False, full=False):
        self.add_day(day)
        out = []
        for t in (full_times(day) if full else times(day)):
            out.append(self.scan(t))
            if shadow:
                SH.step(self.s, self.con, self.env.scope, CFG, registry=TR.REG, now_utc=t, **KW)
        return out

    def prepare(self, target, cfg=CFG, now=None, checks=PASS):
        ro = self.ro()
        try:
            return RR.prepare(self.s, self.con, ro, self.env.scope, target, cfg, registry=TR.REG, risk_cfg=ER.RISK,
                              checks=checks, now=now, out_dir=self.env.tmp / "releases")
        finally:
            ro.close()

    def approve(self, target, h, approval=APP, cfg=CFG, now=None, checks=PASS, guard=NOGUARD):
        return RR.approve(self.s, self.con, self.rw, self.env.scope, target, h, approval, cfg, registry=TR.REG,
                          risk_cfg=ER.RISK, checks=checks, guard=guard, now=now)

    def rollback(self, reason="test", now=None):
        return RR.rollback(self.s, self.con, self.rw, self.env.scope, APP, CFG, reason, guard=NOGUARD, now=now)

    def initial(self, now=None):
        """先按时跑一天常驻纸面运行（纸面运行验收），再建立并批准首次纸面基线。"""
        self.paper_day(LIVE[0], full=True)
        p = self.prepare("INITIAL", now=now)
        assert p["acceptance_passed"], p["manifest"]["acceptance"]
        r = self.approve(self.base["baseline_id"], p["manifest_hash"], now=now or at(LIVE[0], 17))
        assert r["status"] == "ACTIVATED"
        return p

    def candidate(self, to=2, cid=None, now=None):
        """一个已通过正式确认（PASS_FOR_SHADOW）的候选。评测记录直接写入：这里演练的是评测之后的流程。"""
        cid = cid or f"cand_{to:016x}"
        now = now or at(LIVE[0], 17)
        cand = {"candidate_id": cid, "status": "PROPOSED", "scope": str(self.env.scope), "category": "strategy_rule",
                "eval_track": "ALPHA_EXPANSION", "base_release": f"baseline:{self.base['baseline_id']}",
                "evidence_event_ids": ["e1"], "evidence_dates": [DEV[0].isoformat()], "observed_problem": "止损被扫后到目标",
                "hypothesis": "止损多留一格", "exact_diff": [{"target": "strategy_config", "key": KEY, "from": 1, "to": to}],
                "primary_metric": "paired_net_r_per_day", "rollback_trigger": "净 R／日跌破预期区间"}
        with self.con:
            self.con.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                             (cid, str(self.env.scope), "strategy_rule", "ALPHA_EXPANSION", "PASS_FOR_SHADOW",
                              cand["base_release"], json.dumps({"candidate": cand, "meta": {}}), None, now, now))
            c = cands.get(self.con, cid)
            self.con.execute("INSERT OR IGNORE INTO evaluation_protocols VALUES ('proto_t', '{}', 0)")
            self.con.execute("INSERT INTO evaluation_registrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"reg_{cid}", "b1", cid, candidate_digest(c), str(self.env.scope), "ES",
                              self.base["baseline_id"], "rg", "proto_t", "ALPHA_EXPANSION", "ALPHA_EXPANSION", "{}", "{}", now))
            self.con.execute("INSERT INTO evaluation_runs VALUES (?,?,?,?,?,?,?,1,?)",
                             (f"eval_{cid}", cid, str(self.env.scope), "proto_t", "CONFIRM", "PASS_FOR_SHADOW",
                              '{"status":"PASS_FOR_SHADOW"}', now))
        return cid

    def shadow(self, cid, start, now=None, cfg=CFG):
        ro = self.ro()
        try:
            return SH.register(self.s, self.con, ro, self.env.scope, cid, cfg, registry=TR.REG, risk_cfg=ER.RISK,
                               start_day=start.isoformat(), now=now or at(LIVE[0], 18))
        finally:
            ro.close()

    def finish(self, cid, now, cfg=CFG):
        return SH.finish(self.s, self.con, self.env.scope, cid, cfg, risk_cfg=ER.RISK, now=now,
                         out_dir=self.env.tmp / "shadow")

    def monitor(self, day, notify=None, cfg=CFG):
        ro = self.ro()
        try:
            return MON.run(self.s, self.con, ro, self.env.scope, cfg, risk_cfg=ER.RISK, as_of_day=day.isoformat(),
                           now=at(day, 17), notify=notify)
        finally:
            ro.close()

    def active(self):
        return P.resolve(self.rw, self.env.scope)

    def events(self):
        return self.rw.execute("SELECT COUNT(*) FROM release_events").fetchone()[0]


@pytest.fixture
def rel(env):
    r = Rel(env)
    yield r
    r.rw.close()


def refused(fn, *a, **kw):
    with pytest.raises(ReleaseError) as x:
        fn(*a, **kw)
    return x.value.reason


def passed_shadow(rel, cid, days=LIVE[1:3]):
    """登记影子并跑完计划的有效日，结果 PASSED。"""
    reg = rel.shadow(cid, days[0])
    for d in days:
        rel.paper_day(d, shadow=True)
    r = rel.finish(cid, at(days[-1], 18))
    assert r["status"] == "PASSED", r
    return reg, r


# ---------------------------------------------------------------- 配置与守卫
def test_config_validation():
    assert CFG.shadow["window_kind"] == "OPERATIONAL_ACCEPTANCE" and CFG.freeze["approval_release"] == ("RELEASE",)
    for over in ({"shadow.window_kind": "X"}, {"freeze.human_release": ["MANUAL"]},
                 {"monitor.notify_command": "/bin/sh"}, {"approval.approvers": []},
                 {"initial_baseline.regression_groups.vectors": ["../x.py"]}):
        with pytest.raises(ConfigError):
            rcfg(**over)


def test_release_code_guard(rel, tmp_path):
    assert refused(verify_release_code, rel.s) == "RELEASE_GUARD"
    p = rel.s.holdout_root / "protected" / "evaluator-manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"release_sha256": "x"}))
    assert refused(verify_release_code, rel.s) == "RELEASE_GUARD"
    p.write_text(json.dumps({"release_sha256": release_digest(rel.s.project_root), "accepted_by": "eval"}))
    assert verify_release_code(rel.s)["accepted_by"] == "eval"
    # 不传 guard 时批准走默认守卫
    rel.paper_day(LIVE[0])
    h = rel.prepare("INITIAL")["manifest_hash"]
    p.unlink()
    assert refused(RR.approve, rel.s, rel.con, rel.rw, rel.env.scope, rel.base["baseline_id"], h, APP, CFG,
                   registry=TR.REG, risk_cfg=ER.RISK, checks=PASS) == "RELEASE_GUARD"
    assert rel.active() is None


def test_human_approval_checks(rel):
    # 无人工批准：没有确认、不是终端、不是批准人、输错前缀
    assert refused(check_approval, rel.s, CFG, None) == "NO_HUMAN_APPROVAL"
    assert refused(cli_confirm, CFG, "批准", "abcdef1234567890", stdin=io.StringIO("abcdef123456\n"), user="wenye") \
        == "NO_HUMAN_APPROVAL"

    class TTY(io.StringIO):
        def isatty(self):
            return True
    out = io.StringIO()
    assert refused(cli_confirm, CFG, "批准", "abcdef1234567890", stdin=TTY("abcdef123456\n"), out=out, user="mallory") \
        == "NO_HUMAN_APPROVAL"
    assert refused(cli_confirm, CFG, "批准", "abcdef1234567890", stdin=TTY("abcdef12345x\n"), out=out, user="wenye") \
        == "NO_HUMAN_APPROVAL"
    ok = cli_confirm(CFG, "批准", "abcdef1234567890", stdin=TTY("abcdef123456\n"), out=out, user="wenye")
    assert ok == Approval("wenye", "CLI_TTY") and "abcdef123456" in out.getvalue()
    # 测试通道不能写项目的发布库
    import dataclasses
    real = dataclasses.replace(rel.s, release_db=rel.s.project_root / "releases" / "release-state.sqlite")
    assert refused(check_approval, real, CFG, APP) == "NO_HUMAN_APPROVAL"
    assert refused(check_approval, rel.s, CFG, Approval("mallory", "CLI_TTY")) == "NO_HUMAN_APPROVAL"
    # 批准入口本身：没有确认就不动指针
    rel.paper_day(LIVE[0])
    h = rel.prepare("INITIAL")["manifest_hash"]
    assert refused(rel.approve, rel.base["baseline_id"], h, approval=None) == "NO_HUMAN_APPROVAL"
    assert rel.active() is None and rel.events() == 0


def test_release_tables_are_append_only(rel):
    rel.initial()
    for sql in ("UPDATE release_events SET actor='x'", "DELETE FROM release_events",
                "UPDATE release_manifests SET approved_by='x'", "DELETE FROM release_manifests"):
        with pytest.raises(sqlite3.DatabaseError):
            with rel.rw:
                rel.rw.execute(sql)
    with pytest.raises(sqlite3.DatabaseError):
        with rel.con:
            rel.con.execute("DELETE FROM release_version_events")


# ---------------------------------------------------------------- 首次纸面基线
def test_initial_baseline_approval_and_duplicate_refused(rel):
    # 纸面运行验收没过：清单能生成，但不能批准
    p0 = rel.prepare("INITIAL")
    assert not p0["acceptance_passed"] and p0["manifest"]["acceptance"]["paper_run"]["status"] == "NOT_MET"
    assert refused(rel.approve, rel.base["baseline_id"], p0["manifest_hash"]) == "ACCEPTANCE_NOT_MET"
    p = rel.initial()
    m = p["manifest"]
    assert m["release_type"] == "INITIAL_PAPER_BASELINE" and m["previous_release"] is None
    assert m["labels"] == ["UNVALIDATED_BASELINE"] and m["expected"]["source"] == "DEV_BASELINE"
    assert m["baseline_id"] == rel.base["baseline_id"] and m["rollback_target"] is None
    assert set(m["acceptance"]) == {"engineering", "vectors", "ledger", "read_only", "paper_run"}
    act = rel.active()
    assert act["release_id"] == m["release_id"] and act["manifest_hash"] == p["manifest_hash"] and act["entries_enabled"]
    assert RR.version_row(rel.con, m["release_id"])["status"] == "ACTIVE"
    # 同一清单再批准：幂等，不写新事件
    n = rel.events()
    assert rel.approve(rel.base["baseline_id"], p["manifest_hash"])["status"] == "ALREADY_ACTIVE"
    assert rel.events() == n
    # 重复首发：清单生成和发布库事务都拒绝
    assert refused(rel.prepare, "INITIAL") == "ACTIVE_EXISTS"
    other = {**m, "release_id": "rel_other"}
    assert refused(P.activate, rel.rw, rel.env.scope, other, "h", APP) == "ACTIVE_EXISTS"
    assert rel.active()["release_id"] == m["release_id"]


def test_manifest_is_deterministic(rel):
    rel.paper_day(LIVE[0])
    a, b = rel.prepare("INITIAL"), rel.prepare("INITIAL")
    assert a["manifest_hash"] == b["manifest_hash"] and a["release_id"] == b["release_id"]
    assert RM.manifest_hash(a["manifest"]) == a["manifest_hash"]
    md = (rel.env.tmp / "releases" / f"{a['release_id']}.md").read_text(encoding="utf-8")
    assert "UNVALIDATED_BASELINE" in md and a["manifest_hash"] in md


def test_read_only_acceptance_runs_existing_tests(rel):
    r = RM._pytest(rel.s, CFG.regression_groups["read_only"])
    assert r["status"] == "PASS" and "passed" in r["detail"] and " in " not in r["detail"]


# ---------------------------------------------------------------- 影子
def test_shadow_refusals(rel):
    cid = rel.candidate()
    # 没有 active：基线臂没有版本
    assert refused(rel.shadow, cid, LIVE[1]) == "NO_ACTIVE"
    rel.initial()
    # 已披露日期复用拒绝：起始日不晚于已有数据
    assert refused(rel.shadow, cid, LIVE[0]) == "DISCLOSED_DATES"
    # 不是 PASS_FOR_SHADOW
    with rel.con:
        rel.con.execute("INSERT INTO candidates VALUES ('cand_x', ?, 'strategy_rule', 'ALPHA_EXPANSION', 'SCREENED',"
                        " 'baseline:x', '{}', NULL, 1, 0, 0)", (str(rel.env.scope),))
    assert refused(rel.shadow, "cand_x", LIVE[1]) == "NOT_PASS_FOR_SHADOW"
    reg = rel.shadow(cid, LIVE[1])
    assert cands.get(rel.con, cid)["status"] == "SHADOW" and reg["plan"]["valid_days"] == 2
    with pytest.raises(sqlite3.DatabaseError):         # 计划登记后不可修改
        with rel.con:
            rel.con.execute("UPDATE shadow_registrations SET plan_json='{}'")


def test_shadow_plan_scales_with_frequency():
    assert SH.plan_days(CFG, 2.0) == (2, False)
    assert SH.plan_days(CFG, 0.5) == (4, True)       # 要 5 天，上限 4 天，凑不够最低成交数
    assert SH.plan_days(rcfg(**{"shadow.min_fills_per_arm": 15, "shadow.min_valid_days": 20, "shadow.max_valid_days": 60}),
                        0.3) == (60, True)            # ES 开发段约 0.3 笔／日


def test_shadow_endpoint_and_short_sample(rel):
    rel.initial()
    cid = rel.candidate()
    rel.shadow(cid, LIVE[1], cfg=rcfg(**{"shadow.min_fills_per_arm": 50}))
    rel.paper_day(LIVE[1], shadow=True)
    # 不到预登记终点：拒绝，也没有延长的入口
    assert refused(rel.finish, cid, at(LIVE[1], 18)) == "NOT_AT_ENDPOINT"
    rel.paper_day(LIVE[2], shadow=True)
    rel.paper_day(LIVE[3], shadow=True)
    rel.paper_day(LIVE[4], shadow=True)
    r = rel.finish(cid, at(LIVE[4], 18))
    # 短确认样本不足：成交不够最低数 → EVIDENCE_INSUFFICIENT，候选不能准备发布
    assert r["status"] == "EVIDENCE_INSUFFICIENT" and r["reasons"] == ["TOO_FEW_FILLS"] and r["valid_days"] == 4
    assert cands.get(rel.con, cid)["status"] == "EVIDENCE_INSUFFICIENT"
    assert refused(rel.prepare, cid, now=at(LIVE[4], 18)) == "NOT_READY"
    assert rel.finish(cid, at(LIVE[4], 19))["status"] == "ALREADY_FINISHED"


def test_shadow_two_arms_same_clock_and_failure(rel):
    rel.initial()
    cid = rel.candidate()
    reg = rel.shadow(cid, LIVE[1])
    rel.paper_day(LIVE[1], shadow=True)
    rel.paper_day(LIVE[2], shadow=True)
    t = lambda run: [r[0] for r in rel.con.execute("SELECT as_of_utc FROM scan_events WHERE run_id=? ORDER BY as_of_utc",  # noqa: E731
                                                   (run,))]
    base_run, cand_run = SH.arm_run_id(reg["shadow_id"], "base"), SH.arm_run_id(reg["shadow_id"], "cand")
    assert t(base_run) == t(cand_run) and t(base_run)
    orders = lambda run: rel.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=?", (run,)).fetchone()[0]  # noqa: E731
    assert orders(base_run) > 0 and orders(cand_run) > 0
    # 影子未过：候选臂的账本里出现引擎异常标记
    with rel.con:
        rel.con.execute("UPDATE paper_trades SET flags_json='[\"UNPRICEABLE\"]' WHERE run_id=? AND rowid IN"
                        " (SELECT rowid FROM paper_trades WHERE run_id=? LIMIT 1)", (cand_run, cand_run))
    r = rel.finish(cid, at(LIVE[2], 18))
    assert r["status"] == "REJECTED" and "NO_ENGINE_FLAGS:candidate" in r["reasons"]
    assert cands.get(rel.con, cid)["status"] == "REJECTED"
    assert refused(rel.prepare, cid, now=at(LIVE[2], 18)) == "NOT_READY"


def test_forward_holdout_window_is_excluded(rel):
    rel.initial()
    cid = rel.candidate()
    rel.shadow(cid, LIVE[1], cfg=rcfg(**{"shadow.window_kind": "FORWARD_HOLDOUT"}))
    assert SH.reserved_window_start(rel.con, str(rel.env.scope)) == LIVE[1].isoformat()
    from trading_agent.review.build import ReviewError, run as review_run
    with pytest.raises(ReviewError) as x:
        review_run(rel.s, rel.con, rel.env.scope, paper_run_id(rel.env.scope), LIVE[1].isoformat())
    assert x.value.code == "FORWARD_HOLDOUT"
    # 聚合的输入：窗口内已复盘的 session（纵深防御）计入排除，窗口外的照常使用
    from trading_agent.evolve.aggregate import window
    run = paper_run_id(rel.env.scope)
    with rel.con:
        for d in (LIVE[0], LIVE[1]):
            sid = f"{run}:{d}"
            rel.con.execute("INSERT INTO sessions VALUES (?,?,?,'fixture',?,?,'SEALED','{}',0,1,0)",
                            (sid, str(rel.env.scope), d.isoformat(), RF.paper_domain(rel.env.scope), run))
            rel.con.execute("INSERT INTO session_reviews (session_id, scope, trading_day, review_json, review_sha256,"
                            " rules_version, created_at_utc, updated_at_utc) VALUES (?,?,?,'{}','x','r',0,0)",
                            (sid, str(rel.env.scope), d.isoformat()))
    w = window(rel.s, rel.con, str(rel.env.scope), run, 0)
    assert [s["trading_day"] for s in w["sessions"]] == [LIVE[0].isoformat()]
    assert w["excluded"].get("FORWARD_HOLDOUT") == 1


def test_pre_eval_shadow_records_days(rel, monkeypatch):
    rel.initial()
    cid = rel.candidate(cid="cand_00000000000000aa")
    with rel.con:
        rel.con.execute("UPDATE candidates SET status='PROPOSED' WHERE candidate_id=?", (cid,))
    from trading_agent.evolve import queue as Q
    monkeypatch.setattr(Q, "rank", lambda con, scope, cfg, power: [{"candidate_id": cid, "rank": 1}])
    rel.add_day(LIVE[1])
    r = SH.step(rel.s, rel.con, rel.env.scope, CFG, registry=TR.REG, now_utc=times(LIVE[1])[-1], **KW)
    assert [x["candidate_id"] for x in r["pre_eval_shadow"]] == [cid] and r["pre_eval_shadow"][0]["runnable"]
    days = [x[0] for x in rel.con.execute("SELECT trading_day FROM pre_eval_shadow_days WHERE candidate_id=?", (cid,))]
    assert days == [LIVE[1].isoformat()]
    assert "net" not in json.dumps(r["pre_eval_shadow"])          # 不报告盈亏


# ---------------------------------------------------------------- 完整纸面晋级
def test_full_paper_promotion(rel):
    p0 = rel.initial()
    init_id = p0["release_id"]
    cid = rel.candidate()
    reg, res = passed_shadow(rel, cid)
    assert res["checks"]["MIN_FILLS_PER_ARM"] and res["arms"]["candidate"]["trades"] >= 2
    now = at(LIVE[2], 19)
    p = rel.prepare(cid, now=now)
    m = p["manifest"]
    assert m["release_type"] == "CANDIDATE" and m["previous_release"] == init_id == m["rollback_target"]
    assert m["config_diff"] == {KEY: 2} and m["shadow"]["status"] == "PASSED" and m["evaluation"]["status"] == "PASS_FOR_SHADOW"
    assert m["version_tuple"]["strategy_version"].startswith("v1.0+") and "git_commit" in m   # 测试项目不是 git 仓库
    r = rel.approve(cid, p["manifest_hash"], now=now)
    assert r["status"] == "ACTIVATED" and r["replaced"] == init_id
    assert cands.get(rel.con, cid)["status"] == "ACTIVE"
    assert RR.version_row(rel.con, init_id)["status"] == "SUPERSEDED"
    # 下一次扫描起使用新版本
    out = rel.paper_day(LIVE[3])
    vt = [x["version_tuple"] for x in out if x.get("version_tuple")]
    assert vt[-1]["release_id"] == m["release_id"] and vt[-1]["strategy_version"] == m["version_tuple"]["strategy_version"]
    assert out[-1]["release"]["release_id"] == m["release_id"]
    st = RR.status(rel.con, rel.ro(), rel.env.scope)
    assert st["active"]["release_id"] == m["release_id"] and not st["pending"]


def test_tampered_candidate_and_stale_base(rel):
    rel.initial()
    a, b = rel.candidate(2), rel.candidate(3)
    passed_shadow(rel, a)
    rel.shadow(b, LIVE[3])
    rel.paper_day(LIVE[3], shadow=True)
    rel.paper_day(LIVE[4], shadow=True)
    assert rel.finish(b, at(LIVE[4], 18))["status"] == "PASSED"
    now = at(LIVE[4], 19)
    pa, pb = rel.prepare(a, now=now), rel.prepare(b, now=now)
    # 候选哈希被改：准备清单之后改了差异
    row = rel.con.execute("SELECT body_json FROM candidates WHERE candidate_id=?", (a,)).fetchone()[0]
    body = json.loads(row)
    body["candidate"]["exact_diff"][0]["to"] = 4
    with rel.con:
        rel.con.execute("UPDATE candidates SET body_json=? WHERE candidate_id=?", (json.dumps(body), a))
    assert refused(rel.approve, a, pa["manifest_hash"], now=now) == "MANIFEST_MISMATCH"
    assert refused(rel.approve, b, "0" * 64, now=now) == "MANIFEST_MISMATCH"
    # 基线被更新：b 先发布，a 依据的 active 已不是当前版本
    assert rel.approve(b, pb["manifest_hash"], now=now)["status"] == "ACTIVATED"
    with rel.con:
        rel.con.execute("UPDATE candidates SET body_json=? WHERE candidate_id=?", (row, a))
    assert refused(rel.approve, a, pa["manifest_hash"], now=now) == "STALE_BASE"
    assert rel.active()["release_id"] == pb["release_id"]


def test_version_switch_during_scan(rel, monkeypatch):
    rel.initial()
    cid = rel.candidate()
    passed_shadow(rel, cid)
    now = at(LIVE[2], 19)
    p = rel.prepare(cid, now=now)
    rel.add_day(LIVE[3])
    original = scanner.runner.step
    seen = {}

    def step(*a, **kw):
        if not seen:                                   # 扫描已经读了发布库快照，此时另一进程批准新版本
            other = db.open_release(rel.s)
            try:
                seen["r"] = RR.approve(rel.s, rel.con, other, rel.env.scope, cid, p["manifest_hash"], APP, CFG,
                                       registry=TR.REG, risk_cfg=ER.RISK, guard=NOGUARD, now=now)
            finally:
                other.close()
        return original(*a, **kw)
    monkeypatch.setattr(scanner.runner, "step", step)
    first = rel.scan(times(LIVE[3])[0])
    assert seen["r"]["status"] == "ACTIVATED"
    assert first["release"]["release_id"] != p["release_id"]            # 这次扫描整体用旧版本
    monkeypatch.setattr(scanner.runner, "step", original)
    nxt = rel.scan(times(LIVE[3])[1])
    assert nxt["release"]["release_id"] == p["release_id"] and nxt["version_tuple"]["release_id"] == p["release_id"]


# ---------------------------------------------------------------- 冻结与回滚
def test_freeze_blocks_entry_even_after_scan_started(rel, monkeypatch):
    rel.initial()
    rel.add_day(LIVE[1])
    for t in [ER.bar_end(i, LIVE[1]) + 5 for i in range(-1, 5)]:
        rel.scan(t)
    original = scanner.runner.step

    def step(*a, **kw):                                 # 扫描开始后、挂单提交前被冻结
        RF.freeze(rel.con, rel.env.scope, "MANUAL", "演练", "tester")
        return original(*a, **kw)
    monkeypatch.setattr(scanner.runner, "step", step)
    r = rel.scan(ER.bar_end(5, LIVE[1]) + 5)
    assert r["signal_status"] == "SETUP_VALID" and r["risk_status"] == "BLOCKED" and r["block_reason"].startswith("FROZEN:")
    assert rel.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=? AND contract_id IS NOT NULL AND"
                           " signal_bar_end_utc=?", (paper_run_id(rel.env.scope), ER.bar_end(5, LIVE[1]))).fetchone()[0] == 0


def test_freeze_causes_and_policies(rel):
    sc = rel.env.scope
    rel.initial()
    rel.add_day(LIVE[1])
    for t in [ER.bar_end(i, LIVE[1]) + 5 for i in range(-1, 6)]:
        rel.scan(t)                                     # H1 挂单（ARMED）
    r = RF.freeze(rel.con, sc, "MANUAL", "演练", "tester", cancel_working=True)
    assert r["status"] == "FROZEN" and r["risk_domain"] == "*" and len(r["cancelled_orders"]) == 1
    assert rel.con.execute("SELECT status, end_reason FROM paper_orders WHERE order_id=?",
                           (r["cancelled_orders"][0],)).fetchone()[:] == ("CANCELLED", "FROZEN")
    assert RF.freeze(rel.con, sc, "MANUAL", "again", "tester")["status"] == "ALREADY_FROZEN"
    # 解除政策：人工类要确认；RELEASE 只能由新批准；DATA 不能手动
    assert refused(RF.unfreeze, rel.con, sc, "MANUAL", "x", CFG) == "NO_HUMAN_APPROVAL"
    assert refused(RF.unfreeze, rel.con, sc, "RELEASE", "x", CFG, approval=APP) == "POLICY"
    assert refused(RF.unfreeze, rel.con, sc, "DATA", "x", CFG, approval=APP) == "POLICY"
    assert RF.unfreeze(rel.con, sc, "MANUAL", "x", CFG, approval=APP)["status"] == "UNFROZEN"
    # MONITOR 只停常驻纸面运行
    assert RF.freeze(rel.con, sc, "MONITOR", "x", "monitor")["risk_domain"] == RF.paper_domain(sc)
    st = RF.status(rel.con, sc)
    assert st["paper_frozen_reason"] == "FROZEN:MONITOR" and [c["cause"] for c in st["causes"]] == ["MONITOR"]
    # DAILY_LOSS 在下一个交易日自动解除
    RF.freeze(rel.con, sc, "DAILY_LOSS", "日损", "risk")
    assert RF.apply_policies(rel.con, sc, int(time.time())) == []
    assert [x["cause"] for x in RF.apply_policies(rel.con, sc, int(time.time()) + 4 * 86400)] == ["DAILY_LOSS"]


def test_rollback_without_previous_freezes_scope(rel):
    p = rel.initial()
    r = rel.rollback("首次基线演练")
    assert r["status"] == "ENTRIES_DISABLED" and r["to_release"] is None
    act = rel.active()
    assert act["release_id"] == p["release_id"] and not act["entries_enabled"]      # 记录保留
    assert RR.version_row(rel.con, p["release_id"])["status"] == "ROLLED_BACK"
    assert rel.rollback()["status"] == "ALREADY_DISABLED"
    out = rel.paper_day(LIVE[1])
    blocked = [x for x in out if x.get("signal_status") == "SETUP_VALID"]
    assert blocked and all(x["risk_status"] == "BLOCKED" and "RELEASE" in x["block_reason"] for x in blocked)
    assert refused(RF.unfreeze, rel.con, rel.env.scope, "RELEASE", "x", CFG, approval=APP) == "POLICY"


def test_rollback_keeps_open_position_on_its_version(rel):
    p0 = rel.initial()
    cid = rel.candidate()
    passed_shadow(rel, cid)
    now = at(LIVE[2], 19)
    p = rel.prepare(cid, now=now)
    rel.approve(cid, p["manifest_hash"], now=now)
    rel.add_day(LIVE[3])
    for t in [ER.bar_end(i, LIVE[3]) + 5 for i in range(-1, 7)]:
        rel.scan(t)                                     # H1 在候选版本下成交
    run = paper_run_id(rel.env.scope)
    pos = rel.con.execute("SELECT position_id, version_tuple_json FROM paper_positions WHERE run_id=? AND status='OPEN'",
                          (run,)).fetchone()
    assert pos is not None and json.loads(pos[1])["release_id"] == p["release_id"]
    r = rel.rollback("旧仓回滚演练", now=now)
    assert r == {"status": "ROLLED_BACK", "from_release": p["release_id"], "to_release": p0["release_id"]}
    assert rel.active()["release_id"] == p0["release_id"] and cands.get(rel.con, cid)["status"] == "ROLLED_BACK"
    for t in [ER.bar_end(i, LIVE[3]) + 5 for i in range(7, 14)] + [times(LIVE[3])[-1]]:
        rel.scan(t)
    trade = rel.con.execute("SELECT exit_reason, version_tuple_json FROM paper_trades WHERE trade_id=?", (pos[0],)).fetchone()
    assert trade is not None and json.loads(trade[1])["release_id"] == p["release_id"]     # 原版本、原止损规则完成
    later = [json.loads(v)["release_id"] for (v,) in rel.con.execute(
        "SELECT version_tuple_json FROM paper_orders WHERE run_id=? AND signal_bar_end_utc>?", (run, ER.bar_end(7, LIVE[3])))]
    assert later and set(later) == {p0["release_id"]}


def test_parallel_scopes_are_independent(rel):
    p = rel.initial()
    nq = parse_scope("pa_brooks:NQ:RTH:5m", rel.s)
    m = {**p["manifest"], "release_id": "rel_nq", "scope": {**p["manifest"]["scope"], "instrument_root": "NQ"}}
    assert P.activate(rel.rw, nq, m, "hnq", APP)["status"] == "ACTIVATED"
    assert P.resolve(rel.rw, nq)["release_id"] == "rel_nq" and rel.active()["release_id"] == p["release_id"]
    assert rel.rollback()["status"] == "ENTRIES_DISABLED"
    assert P.resolve(rel.rw, nq)["entries_enabled"] and not rel.active()["entries_enabled"]
    assert RF.freeze(rel.con, nq, "MANUAL", "x", "t")["status"] == "FROZEN"
    assert RF.status(rel.con, rel.env.scope)["causes"][0]["cause"] == "RELEASE"


# ---------------------------------------------------------------- 发布后监控
def test_monitor_freezes_on_breach(rel):
    p = rel.initial(now=at(LIVE[0], 17))
    assert rel.monitor(LIVE[0])["status"] == "OK"          # 激活当天之后才有数据
    sent = []
    rel.paper_day(LIVE[1])
    r1 = rel.monitor(LIVE[1], notify=sent.append)
    assert r1["status"] == "OK" and r1["metrics"]["valid_days"] == 1 and not sent
    for d in LIVE[2:5]:
        rel.paper_day(d)
    r = rel.monitor(LIVE[4], notify=sent.append)
    trig = p["manifest"]["rollback_trigger"]
    assert r["status"] == "BREACHED" and r["action"] == "FROZEN" and r["unvalidated"]
    assert "DRAWDOWN" in [b["trigger"] for b in r["breaches"]] and r["metrics"]["drawdown"] > trig["drawdown_limit"]
    assert len(sent) == 1 and "MONITOR" in sent[0]
    assert RF.status(rel.con, rel.env.scope)["paper_frozen_reason"] == "FROZEN:MONITOR"
    assert rel.monitor(LIVE[4], notify=sent.append)["status"] == "ALREADY_RUN" and len(sent) == 1
    # 冻结后新信号被挡；是否回滚由人决定
    out = rel.paper_day(LIVE[5])
    assert all(x["risk_status"] == "BLOCKED" for x in out if x.get("signal_status") == "SETUP_VALID")
    assert rel.active()["entries_enabled"]


def test_monitor_breach_rules():
    trig = {"rolling_valid_days": 2, "rolling_net_per_day_floor": -1.0, "drawdown_limit": 5.0,
            "trades_per_day_band": [0.5, 4.0], "setups_per_day_band": None, "ambiguous_bar_share_max": 0.05,
            "ambiguous_min_trades": 3}
    base = {"rolling_days": 2, "rolling_net_per_day": 0.0, "drawdown": 1.0, "trades_per_day": 1.0, "setups_per_day": 9,
            "trades": 4, "ambiguous_bar_share": 0.0}
    assert MON.breaches(base, trig) == []
    got = lambda **kw: [b["trigger"] for b in MON.breaches({**base, **kw}, trig)]  # noqa: E731
    assert got(rolling_net_per_day=-2) == ["ROLLING_NET"] and got(drawdown=6) == ["DRAWDOWN"]
    assert got(trades_per_day=5) == ["TRADE_FREQUENCY"] and got(ambiguous_bar_share=0.5) == ["AMBIGUOUS_BAR"]
    assert got(rolling_days=1, trades_per_day=5, rolling_net_per_day=-2) == []   # 窗口不满：频率和滚动净值不判定
    assert got(trades=2, ambiguous_bar_share=0.5) == []


# ---------------------------------------------------------------- 命令行
def test_cli_release_commands(env, capsys):
    def run(*argv):
        code = main([*argv, "--json"], settings=env.settings)
        return code, json.loads(capsys.readouterr().out)
    code, r = run("release", "--status")
    assert code == 0 and r["active"] is None
    code, r = run("monitor")
    assert code == 0 and r["status"] == "NO_ACTIVE"
    code, r = run("rollback")
    assert code == 1 and r["reason"] == "NO_ACTIVE"
    code, r = run("release", "--approve", "INITIAL", "--manifest-hash", "ab" * 32)
    assert code == 1 and r["reason"] == "NO_HUMAN_APPROVAL"                  # 不是终端：代理跑不过这一步
    assert not env.settings.release_db.exists()
    code, r = run("freeze", "--reason", "演练")
    assert code == 0 and r["status"] == "FROZEN" and r["cause"] == "MANUAL"
    code, r = run("freeze", "--unfreeze")
    assert code == 1 and r["reason"] == "NO_HUMAN_APPROVAL"
    code, r = run("freeze", "--status")
    assert code == 0 and r["frozen"]
    code, r = run("shadow", "--finish", "cand_none")
    assert code == 1 and r["reason"] == "NO_SHADOW"


def test_release_package_boundaries():
    """只有 pointer.py 写发布库；release/ 里没有下单、信号或成交逻辑。"""
    import pathlib
    import re
    root = pathlib.Path(H.PROJECT) / "src" / "trading_agent"
    writes = re.compile(r"(INSERT|UPDATE|DELETE)[^\"']*\b(active_pointers|release_events|release_manifests)\b")
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if writes.search(text):
            assert f.name == "pointer.py" and f.parent.name == "release", f
    for f in (root / "release").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for banned in ("on_bar(", "apply_bar(", "import fills", "requests", "socket"):
            assert banned not in text, (f, banned)


def test_paper_run_acceptance_counts_only_timely_days(rel):
    rel.add_day(LIVE[0])
    rel.scan(sessions.rth_bounds(LIVE[0])[1] + 3600)          # 盘后一次性补跑：信号全过期，不算运行过
    rel.paper_day(LIVE[1])                                     # 只有上午逐根推进，其余补跑：不到一半按时
    rel.paper_day(LIVE[2], full=True)
    assert RM.paper_run_days(rel.con, rel.env.scope) == [LIVE[2].isoformat()]
