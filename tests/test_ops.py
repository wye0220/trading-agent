"""阶段 10：调度、告警、控制通道与恢复。全部在临时库上（合成行情、合成券商快照、测试发布库）。"""
import copy
import json
import sqlite3

import pytest
import yaml

import helpers as H
import manual_helpers as M
import test_engine_runner as ER
import test_llm as TL
import test_release as TRL
import test_research as TR
from trading_agent import sessions
from trading_agent.engine import llm
from trading_agent.engine.config import load_engine
from trading_agent.manual.common import paper_run_id
from trading_agent.ops import adapters as AD
from trading_agent.ops import alerts as AL
from trading_agent.ops import control as C
from trading_agent.ops import daily as DY
from trading_agent.ops import explain as EX
from trading_agent.ops import health as HL
from trading_agent.ops import state as ST
from trading_agent.ops import tick as TK
from trading_agent.ops.config import parse_control, parse_ops
from trading_agent.release import freeze as RF

OPS_RAW = yaml.safe_load((H.TEST_PROJECT / "config" / "ops.yaml").read_text(encoding="utf-8"))
CTL_RAW = yaml.safe_load((H.TEST_PROJECT / "config" / "control.yaml").read_text(encoding="utf-8"))
D1 = ER.D1
KW = dict(risk_cfg=ER.RISK, engine_cfg=ER.ENG)


def ocfg(**over):
    raw = copy.deepcopy(OPS_RAW)
    raw["alerts"]["notifier"] = "none"
    raw["daily"]["account"] = M.ACCT
    for k, v in over.items():
        node = raw
        ks = k.split(".")
        for x in ks[:-1]:
            node = node[x]
        node[ks[-1]] = v
    return parse_ops(raw)


def ccfg(**over):
    raw = copy.deepcopy(CTL_RAW)
    raw["adapters"]["cli"]["users"] = {"trader": {"approver": True}, "intern": {"approver": False}}
    raw["adapters"]["telegram"].update(enabled=True, chat_id="-100", users={"42": {"approver": True}})
    raw.update(over)
    return parse_control(raw)


OCFG, CCFG = ocfg(), ccfg()
rel = TRL.rel                                                            # 复用阶段 9 的临时发布环境


def tick(env, now, cfg=OCFG, shadow=False):
    return TK.run(env.settings, env.con, cfg, registry=TR.REG, release_cfg=TRL.CFG, now=now, shadow=shadow, **KW)


def ctl(env, text, user="trader", channel="CLI", ref=None, now=None, verified=True, guard=TRL.NOGUARD, cfg=CCFG):
    return C.execute(env.settings, env.con, C.Message(channel, user, text, ref, verified), cfg, OCFG,
                     registry=TR.REG, release_guard=guard, now=now)


def alerts(env, kind=None):
    q = "SELECT level, kind, message FROM alerts" + (" WHERE kind=?" if kind else "") + " ORDER BY alert_id"
    return [tuple(r) for r in env.con.execute(q, (kind,) if kind else ())]


# ---------------------------------------------------------------- 配置、锁、状态
def test_config_validation():
    assert OCFG.scopes == ("pa_brooks:ES:RTH:5m",) and OCFG.silence_for("DATA_STALE") == 900
    assert OCFG.silence_for("ANYTHING") == 1800 and CCFG.users("CLI")["trader"]["approver"]
    from trading_agent.config import ConfigError
    for over in ({"alerts.notifier": "email"}, {"scopes": ["ES"]}, {"tick.late_bars_critical": 0}):
        with pytest.raises(ConfigError):
            ocfg(**over)
    with pytest.raises(ConfigError):
        parse_control({"adapters": {"telegram": {"enabled": True}}})


def test_run_lock(env):
    assert ST.acquire(env.con, "x", 60, now=100, who="a")
    assert not ST.acquire(env.con, "x", 60, now=120, who="b")          # 重复运行被挡
    assert ST.acquire(env.con, "x", 60, now=170, who="b")              # 过期的锁可接管
    ST.release(env.con, "x", who="b")
    assert ST.locks(env.con, 200) == []


# ---------------------------------------------------------------- 盘中节拍
def test_tick_dedupes_by_watermark_and_lock(env):
    ER.load(env, ER.scripted_day())
    t = ER.bar_end(5) + 5
    r = tick(env, t)
    assert r["status"] == "OK" and r["scopes"][0]["status"] == "SCANNED"
    assert r["scopes"][0]["signal_status"] == "SETUP_VALID" and r["scopes"][0]["risk_status"] == "PASS"
    again = tick(env, t + 30)
    assert again["scopes"][0]["status"] == "NO_NEW_BAR"                 # 同一根线不重复处理
    assert ST.get(env.con, "heartbeat:tick")["at_utc"] == t + 30
    assert ST.get(env.con, "watermark:pa_brooks:ES:RTH:5m")["bar_end_utc"] == ER.bar_end(5)
    ST.acquire(env.con, TK.LOCK, 600, now=t + 60, who="other")
    assert tick(env, t + 60)["status"] == "LOCKED"


def test_tick_catch_up_does_not_revive_expired_orders(env):
    ER.load(env, ER.scripted_day())
    tick(env, ER.bar_end(4) + 5)
    r = tick(env, ER.bar_end(12) + 5)                                     # 睡了 40 分钟
    assert r["scopes"][0]["status"] == "SCANNED"
    run = paper_run_id(env.scope)
    late = [json.loads(x[0]) for x in env.con.execute("SELECT decision_json FROM scan_events WHERE run_id=?", (run,))]
    assert any(d.get("catch_up") for d in late)
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=?", (run,)).fetchone()[0] == 0


def test_tick_data_stale_alerts(env):
    ER.load(env, [r for r in ER.scripted_day() if int(r["bar_start_utc"] if str(r["bar_start_utc"]).isdigit() else
                                                       sessions.parse_utc(r["bar_start_utc"])) < ER.bar_end(2)])
    tick(env, ER.bar_end(3) + 100)                                        # 落后 1 根：WARN
    tick(env, ER.bar_end(7) + 100)                                        # 落后 5 根：CRITICAL
    kinds = [(lv, k) for lv, k, _ in alerts(env) if k in ("LATE_BAR", "DATA_STALE")]
    assert ("WARN", "LATE_BAR") in kinds and ("CRITICAL", "DATA_STALE") in kinds


# ---------------------------------------------------------------- 盘后与周度
def test_daily_and_weekly(env):
    ER.load(env, ER.scripted_day())
    for i in range(-1, 14):
        tick(env, ER.bar_end(i) + 5)
    close = sessions.rth_bounds(D1)[1]
    tick(env, close + 600)
    early = DY.run_daily(env.settings, env.con, OCFG, registry=TR.REG, release_cfg=TRL.CFG, manual_cfg=M.MAN,
                         now=close + 60, **KW)
    assert early["status"] == "TOO_EARLY"
    r = DY.run_daily(env.settings, env.con, OCFG, registry=TR.REG, release_cfg=TRL.CFG, manual_cfg=M.MAN,
                     now=close + 16 * 60, **KW)
    s = r["scopes"][0]
    assert r["status"] == "DONE" and s["ledger"] == "PASS" and s["monitor"] == "NO_ACTIVE" and s["review"] != "NO_PAPER_RUN"
    assert env.con.execute("SELECT status FROM sessions WHERE session_id=?",
                           (f"{paper_run_id(env.scope)}:{D1}",)).fetchone()[0] == "SEALED"
    assert ST.get(env.con, "heartbeat:daily")["trading_day"] == D1.isoformat()
    nxt = sessions.next_trading_day(D1)
    miss = DY.run_daily(env.settings, env.con, OCFG, registry=TR.REG, release_cfg=TRL.CFG, manual_cfg=M.MAN,
                        day=nxt.isoformat(), now=sessions.rth_bounds(nxt)[1] + 3600, **KW)
    assert miss["scopes"][0]["status"] == "INCOMPLETE_DAY" and alerts(env, "INCOMPLETE_DAY")
    assert DY.run_weekly(env.settings, env.con, OCFG, registry=TR.REG, manual_cfg=M.MAN,
                         day=D1.isoformat())["status"] == "NOT_LAST_TRADING_DAY"     # 2025-11-25 是星期二
    assert DY.is_last_trading_day_of_week(sessions.next_trading_day(sessions.next_trading_day(D1)))  # 感恩节前的 26 日之后是 28 日


# ---------------------------------------------------------------- 告警
def test_dispatch_backlog_dedup_and_levels(env):
    ST.alert(env.con, "CRITICAL", "OLD", "启用之前的告警", now=1)
    sent = []
    notify = lambda text: sent.append(text) or "SENT"           # noqa: E731
    r = AL.dispatch(env.con, OCFG, notify, now=1000)
    assert r["dispatched"] == 0 and not sent                     # 积压的不推送
    ST.alert(env.con, "CRITICAL", "DATA_STALE", "断流", scope="s", now=1001)
    ST.alert(env.con, "CRITICAL", "DATA_STALE", "断流", scope="s", now=1002)
    ST.alert(env.con, "INFO", "NEW_CANDIDATES", "新候选", now=1003)
    r = AL.dispatch(env.con, ocfg(**{"alerts.push_levels": ["CRITICAL", "WARN"]}), notify, now=1005)
    assert r["by_status"] == {"SENT": 1, "SUPPRESSED": 1, "NOT_PUSHED": 1} and len(sent) == 1 and "DATA_STALE" in sent[0]
    ST.alert(env.con, "CRITICAL", "DATA_STALE", "断流", scope="s", now=3000)
    assert AL.dispatch(env.con, OCFG, notify, now=3000)["by_status"] == {"SENT": 1}   # 静默窗口过了再推
    ST.alert(env.con, "WARN", "X", "y", now=3001)
    assert AL.dispatch(env.con, OCFG, lambda t: "FAILED:1", now=3002)["by_status"] == {"FAILED": 1}


def test_watch_stopped_is_critical(env):
    t = H.et(D1, 11, 0)
    r = AL.watch_check(env.con, env.settings, M.ACCT, t)
    assert r["status"] == "WATCH_STOPPED" and alerts(env, "WATCH_STOPPED")[0][0] == "CRITICAL"
    M.snapshot(env, t)
    M.poll(env, t)
    assert AL.watch_check(env.con, env.settings, M.ACCT, t + 30)["status"] == "OK"
    assert AL.watch_check(env.con, env.settings, M.ACCT, H.et(D1, 20, 0))["status"] == "OUT_OF_SESSION"


# ---------------------------------------------------------------- 模型调用与死信
def test_explain_schema_retries_dead_letter_and_replay(env):
    eng = load_engine(H.TEST_PROJECT)
    rid = llm.enqueue_explanation(env.con, "ScanExplanation", {"event_id": "scan_1"}, scope=str(env.scope),
                                  snapshot_id="snap_1", event_id="scan_1")
    bad = lambda p: '{"summary": "缺字段"}'                    # noqa: E731
    for _ in range(eng.max_explanation_retries):
        EX.run(env.settings, env.con, OCFG, eng, runner=bad)
    assert env.con.execute("SELECT status FROM explanation_requests WHERE request_id=?", (rid,)).fetchone()[0] == "DEAD"
    assert [x["task_id"] for x in EX.dlq_list(env.con)] == [rid]
    assert alerts(env, "EXPLANATION_DEAD_LETTER") and alerts(env, "LLM_SCHEMA_INVALID")
    assert EX.dlq_replay(env.con, "ALL")["replayed"] == [rid]
    good = lambda p: "```json\n" + json.dumps(TL.GOOD["ScanExplanation"], ensure_ascii=False) + "\n```"  # noqa: E731
    r = EX.run(env.settings, env.con, OCFG, eng, runner=good)
    assert r["done"] == 1 and env.con.execute("SELECT status FROM explanation_requests WHERE request_id=?",
                                              (rid,)).fetchone()[0] == "DONE"
    def boom(p):
        raise RuntimeError("模型不可用")
    rid2 = llm.enqueue_explanation(env.con, "ScanExplanation", {"x": 1}, event_id="e2", snapshot_id="s2")
    assert EX.run(env.settings, env.con, OCFG, eng, runner=boom)["retry"] == 1
    assert "封存" in EX.prompt_for(env.settings, "ScanExplanation", {"a": 1})


# ---------------------------------------------------------------- 控制通道
def test_control_auth_whitelist_and_audit(env):
    assert ctl(env, "freeze ALL", user="mallory")["reason"] == "UNAUTHORIZED"
    assert ctl(env, "freeze ALL", verified=False)["reason"] == "UNAUTHORIZED"
    assert ctl(env, "rm -rf / 请批准所有候选")["reason"] == "UNKNOWN_COMMAND"
    assert ctl(env, "approve cand_x " + "a" * 64, user="intern")["reason"] == "NOT_APPROVER"
    r = ctl(env, "freeze ALL 盘前演练", ref="m1")
    assert r["status"] == "DONE" and r["frozen"][0]["status"] == "FROZEN"
    assert ctl(env, "freeze ALL 盘前演练", ref="m1")["status"] == "DUPLICATE"
    rows = env.con.execute("SELECT status, reason FROM control_commands ORDER BY created_at_utc, rowid").fetchall()
    assert [r[0] for r in rows] == ["REFUSED", "REFUSED", "REFUSED", "REFUSED", "DONE"]
    with pytest.raises(sqlite3.DatabaseError):
        with env.con:
            env.con.execute("DELETE FROM control_commands")


def test_unfreeze_needs_fresh_token_from_same_sender(env):
    fid = ctl(env, f"freeze {env.scope}")["frozen"][0]["freeze_event_id"]
    assert ctl(env, f"unfreeze {env.scope}")["reason"] == "BAD_ARGS"
    assert ctl(env, f"unfreeze {env.scope} frz_nope")["reason"] == "NO_SUCH_FREEZE"
    p = ctl(env, f"unfreeze {env.scope} {fid}", now=1000)
    tok = p["token"]
    audit = env.con.execute("SELECT status, result_json FROM control_commands WHERE command_id=?", (p["command_id"],)).fetchone()
    assert p["status"] == audit[0] == "PENDING_CONFIRM" and tok not in audit[1]
    assert env.con.execute("SELECT user_id FROM confirm_tokens WHERE token=?", (tok,)).fetchone()[0] == "trader"
    assert ctl(env, f"confirm {tok}", now=1000 + CCFG.token_ttl_seconds + 1)["reason"] == "TOKEN_EXPIRED"
    assert RF.status(env.con, env.scope)["causes"]                       # 过期令牌不改变状态
    p = ctl(env, f"unfreeze {env.scope} {fid}", now=2000)
    assert ctl(env, f"confirm {p['token']}", user="intern", now=2001)["reason"] in ("NOT_APPROVER", "TOKEN_MISMATCH")
    assert ctl(env, f"confirm {p['token']}", channel="TELEGRAM", user="42", now=2001)["reason"] == "TOKEN_MISMATCH"
    ok = ctl(env, f"confirm {p['token']}", now=2002)
    assert ok["status"] == "DONE" and ok["result"] == "UNFROZEN" and not RF.status(env.con, env.scope)["causes"]
    assert ctl(env, f"confirm {p['token']}", now=2003)["reason"] == "TOKEN_USED"


def test_watch_interval_switch(env):
    from trading_agent.manual import watch as W
    assert ctl(env, "watch-interval 2m")["reason"] == "BAD_ARGS"
    assert ctl(env, "watch-interval 5m")["seconds"] == 300
    assert ST.get(env.con, "watch_interval_override")["seconds"] == 300
    seq, slept = iter([300, 60]), []
    W.run_loop(lambda: {}, 60, iterations=3, sleep=slept.append, interval_fn=lambda: next(seq))
    assert slept == [300, 60]                                           # 每轮重读
    assert ctl(env, "watch-interval 1m")["seconds"] == 60


def test_context_only_tightens(env):
    now = M.paper_state(env, 4)
    assert ctl(env, "context 14:00 FOMC 前不开新仓", now=now)["reason"] == "BAD_ARGS"     # 自由文本只当数据
    r = ctl(env, f"context no-entry until 15:00 {env.scope}", now=now)
    assert r["status"] == "DONE" and r["kind"] == "NO_ENTRY"
    p = M.mkplan(env, now=now + 1)
    assert p["check"]["conclusion"] == "BLOCKED_BY_RISK" and p["check"]["risk_reason"] == "CONTEXT_NO_ENTRY"
    assert "CONTEXT" in RF.status(env.con, env.scope)["paper_frozen_reason"]
    # 到期后由政策自动解除
    RF.apply_policies(env.con, env.scope, H.et(D1, 15, 1))
    assert not any(c["cause"] == "CONTEXT" for c in RF.status(env.con, env.scope)["causes"])
    later = H.et(D1, 15, 5)                                              # 禁止入场的约束已到期
    r = ctl(env, "context max-qty 1", now=later)
    assert r["value"] == {"max_qty": 1}
    p2 = M.mkplan(env, "ES L 5000 4996 5008 2 test", now=later + 2)
    assert p2["check"]["risk_reason"] == "CONTEXT_MAX_QTY:1"
    assert ctl(env, "context max-qty 0", now=later)["reason"] == "BAD_ARGS"


def test_control_note_reject_and_plan(env):
    r = ctl(env, "note 止损是手动挪的，不是系统")
    assert r["status"] == "DONE" and r["event_id"] is None
    assert env.con.execute("SELECT author FROM trader_notes WHERE note_id=?", (r["note_id"],)).fetchone()[0] == "control:cli:trader"
    assert ctl(env, "reject rel_nope")["reason"] == "NOT_PENDING"
    assert ctl(env, "plan ES")["reason"] == "BAD_ARGS"


def test_telegram_adapter_verifies_chat_and_sender(env):
    calls = []

    def transport(method, params):
        calls.append((method, params))
        return {"result": [
            {"update_id": 7, "message": {"chat": {"id": -100}, "from": {"id": 42}, "text": "freeze ALL"}},
            {"update_id": 8, "message": {"chat": {"id": -100}, "from": {"id": 99}, "text": "freeze ALL"}},
            {"update_id": 9, "message": {"chat": {"id": -5}, "from": {"id": 42}, "text": "freeze ALL"}}]}
    assert AD.telegram_poll(env.con, CCFG, transport) == []         # 第一次只记位置：启用前积压的消息不当命令
    assert ST.get(env.con, AD.OFFSET_KEY) == 9
    ST.put(env.con, AD.OFFSET_KEY, 6)
    msgs = AD.telegram_poll(env.con, CCFG, transport)
    assert [m.verified for m in msgs] == [True, False, False] and ST.get(env.con, AD.OFFSET_KEY) == 9
    res = [C.execute(env.settings, env.con, m, CCFG, OCFG, registry=TR.REG)["status"] for m in msgs]
    assert res == ["DONE", "REFUSED", "REFUSED"]
    AD.telegram_poll(env.con, CCFG, transport)
    assert calls[-1][1]["offset"] == 10
    assert AD.normalize("/watch_interval@ta_bot 5m") == "watch-interval 5m" and AD.normalize("freeze ALL") == "freeze ALL"


def test_telegram_bind_and_resolve(env, monkeypatch):
    monkeypatch.setattr(AD, "shared_env_files", lambda s: [])
    assert AD.telegram_credentials(env.settings) is None
    sec = env.settings.data_root / "secrets"
    sec.mkdir(parents=True, exist_ok=True)
    (sec / "telegram.env").write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=-100\n", encoding="utf-8")
    monkeypatch.setattr(AD, "shared_env_files", lambda s: [sec / "telegram.env"])
    assert AD.telegram_credentials(env.settings) == ("t", "-100")
    cfg = parse_control({"adapters": {"telegram": {"enabled": True, "chat_id": "from_env", "users": "from_secrets",
                                                   "approver": True}}})
    assert AD.resolve(env.settings, cfg).telegram_users == {}
    sent, batches = [], iter([
        {"result": [{"update_id": 1, "message": {"chat": {"id": -100}, "from": {"id": 5}, "text": "/freeze ALL"}}]},
        {"result": [{"update_id": 2, "message": {"chat": {"id": -100}, "from": {"id": 77}, "text": "hi"}},
                    {"update_id": 3, "message": {"chat": {"id": -100}, "from": {"id": 42}, "text": "/bind@ta_bot c0de"}}]}])

    def transport(method, params):
        if method == "sendMessage":
            sent.append(params)
            return {"ok": True}
        if method == "getMe":
            return {"result": {"username": "ta_bot"}}
        return next(batches)
    r = AD.bind(env.settings, env.con, AD.resolve(env.settings, cfg), transport, code="c0de", sleep=lambda s: None,
                emit=lambda s: None)
    assert r["status"] == "BOUND" and r["user_id_suffix"] == "42" and sent and sent[0]["chat_id"] == "-100"
    import stat
    assert stat.S_IMODE((sec / "control-telegram.env").stat().st_mode) == 0o600
    got = AD.resolve(env.settings, cfg)
    assert got.telegram_chat_id == "-100" and got.telegram_users == {"42": {"approver": True}}
    assert ST.get(env.con, AD.OFFSET_KEY) == 3                           # 绑定期间的消息没有被当命令执行


def test_health_lists_recovery(env):
    ST.heartbeat(env.con, "tick", 100)
    ST.acquire(env.con, "tick", 10, now=100, who="dead")
    r = HL.report(env.con, now=200)
    assert r["heartbeats"]["tick"]["age_seconds"] == 100 and r["locks"][0]["expired"]
    assert {"失去行情或迟到", "停机或睡眠之后", "重复运行", "控制通道离线", "券商只读接口失效", "死信队列"} == set(r["recovery"])


# ---------------------------------------------------------------- 全链路演示
def test_demo_phase_one(env):
    """一期：ingest → scan（节拍）→ plan（控制通道路由到预检）→ watch（1m／5m 切换、合成快照触发提醒、即时推送）
    → 收盘封存 → 纪律 KPI。"""
    AL.dispatch(env.con, OCFG, lambda t: "SENT", now=1)                  # 启用分发（之前的告警标为积压）
    ER.load(env, ER.scripted_day())
    for i in range(-1, 6):
        tick(env, ER.bar_end(i) + 5)
    now = ER.bar_end(5) + 30
    p = M.mkplan(env, now=now)
    assert M.activate(env, p["plan_id"], now=now + 1)["status"] == "ACTIVE"
    assert ctl(env, "watch-interval 1m")["seconds"] == 60
    iv = ST.get(env.con, "watch_interval_override")["seconds"]
    M.snapshot(env, now + 60, positions=[M.pos(1)])                     # 有持仓没有保护止损：先开始计时
    M.poll(env, now + 60, interval=iv)
    M.snapshot(env, now + 125, positions=[M.pos(1)])                    # 宽限期过了：NAKED_POSITION
    w = M.poll(env, now + 125, interval=iv)
    assert "NAKED_POSITION" in [a["rule"] for a in w["alerts"]]
    sent = []
    AL.dispatch(env.con, OCFG, lambda t: sent.append(t) or "SENT", now=now + 126)
    assert any("NAKED_POSITION" in s for s in sent), (sent, w["alerts"])
    assert ctl(env, "watch-interval 5m")["seconds"] == 300
    M.snapshot(env, now + 400, positions=[M.pos(1)], orders=[M.stop_order(4996)])
    w2 = M.poll(env, now + 400, interval=300)
    assert w2["status"] == "OK"
    for i in range(6, 14):
        tick(env, ER.bar_end(i) + 5)
    close = sessions.rth_bounds(D1)[1]
    tick(env, close + 600)
    d = DY.run_daily(env.settings, env.con, OCFG, registry=TR.REG, release_cfg=TRL.CFG, manual_cfg=M.MAN,
                     now=close + 20 * 60, **KW)
    assert d["status"] == "DONE" and d["account"] == M.ACCT
    from trading_agent.manual import discipline
    k = discipline.session_kpis(env.con, M.MAN, M.ACCT, D1.isoformat())
    assert "plan_coverage" in k and k["alerts"].get("NAKED_POSITION", {}).get("count", 0) >= 1


def test_demo_phase_two_release_chain(rel, monkeypatch):
    """二期的发布段：首次基线 → 影子 → 控制通道批准（回显 manifest_hash）→ 盘后监控 → 冻结 → 回滚 → 并行 scope。
    复盘、聚合、评测各段的演练在 test_review、test_evolve、test_evaluation（见 docs/ops-runbook.md）。"""
    env = rel.env
    from trading_agent.release import config as RC
    monkeypatch.setattr(RC, "load_release", lambda root: TRL.CFG)        # 控制通道与准备清单用同一份发布政策
    p0 = rel.initial()
    cid = rel.candidate()
    TRL.passed_shadow(rel, cid)
    now = TRL.at(TRL.LIVE[2], 19)
    p = rel.prepare(cid, now=now)
    assert ctl(env, f"approve {cid} {p['manifest_hash'][:12]}", now=now)["reason"] == "BAD_ARGS"    # 要完整回显
    a = ctl(env, f"approve {cid} {p['manifest_hash']}", now=now)
    assert a["status"] == "DONE" and a["release_id"] == p["release_id"], a
    assert env.con.execute("SELECT detail_json FROM release_version_events WHERE release_id=? AND to_status='APPROVED'",
                           (p["release_id"],)).fetchone() is not None
    for d in TRL.LIVE[3:7]:
        rel.paper_day(d)
    cfg = ocfg()
    r = DY.run_daily(env.settings, env.con, cfg, registry=TR.REG, release_cfg=TRL.CFG, manual_cfg=M.MAN,
                     day=TRL.LIVE[6].isoformat(), now=sessions.rth_bounds(TRL.LIVE[6])[1] + 3600, **KW)
    assert r["scopes"][0]["monitor"] in ("OK", "BREACHED")
    if r["scopes"][0]["monitor"] == "BREACHED":
        assert alerts(env, "MONITOR_FREEZE")
    rb = rel.rollback("演练", now=now)
    assert rb["to_release"] == p0["release_id"]
    # 双策略并行隔离：另一个 scope 的冻结和指针不受影响
    from trading_agent.config import parse_scope
    nq = parse_scope("pa_brooks:NQ:RTH:5m", env.settings)
    assert ctl(env, "freeze pa_brooks:NQ:RTH:5m 演练")["status"] == "DONE"
    assert not any(c["cause"] == "MANUAL" for c in RF.status(env.con, env.scope)["causes"])
    assert RF.status(env.con, nq)["causes"][0]["cause"] == "MANUAL"


def test_ops_scripts_are_samples_and_read_only():
    import re
    d = H.PROJECT / "ops" / "launchd"
    intraday = (d / "run-intraday.sh").read_text(encoding="utf-8")
    allowed = intraday.split("--allowedTools", 1)[1].split("--model", 1)[0]
    assert "get_futures_bars" in allowed and "collect --from-mcp" in allowed
    for banned in ("place_", "revoke_", "cancel", "order", "Write", "Edit", "Bash(*)", "dangerously"):
        assert banned not in allowed, banned
    texts = "".join(p.read_text(encoding="utf-8") for p in d.glob("run-*.sh"))
    assert not re.search(r"\d{6,}:[A-Za-z0-9_-]{20,}", texts)                    # 没有写死的 token
    new = sorted(p.name for p in d.glob("com.wenye.trading-agent-*.plist*") if "collect" not in p.name)
    assert new and all(n.endswith(".plist.example") for n in new)               # 只有样例，没有可直接加载的 plist
    explain = (H.PROJECT / "src" / "trading_agent" / "ops" / "explain.py").read_text(encoding="utf-8")
    assert '"--tools", ""' in explain                                            # 模型调用不给任何工具


def test_daily_catches_up_paper_run_without_intraday_ticks(env):
    """没有盘中节拍时，盘后先补扫当天：纸面运行有完整记录，但补扫的信号不挂单、不算按时处理。"""
    from trading_agent.release import manifest as RM
    ER.load(env, ER.scripted_day())
    close = sessions.rth_bounds(D1)[1]
    r = DY.run_daily(env.settings, env.con, OCFG, registry=TR.REG, release_cfg=TRL.CFG, manual_cfg=M.MAN,
                     now=close + 90 * 60, **KW)
    s = r["scopes"][0]
    assert s["catch_up"] == "OK" and s["status"] == "DONE" and s["ledger"] == "PASS"
    run = paper_run_id(env.scope)
    assert env.con.execute("SELECT COUNT(*) FROM scan_events WHERE run_id=?", (run,)).fetchone()[0] > 0
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=?", (run,)).fetchone()[0] == 0
    assert RM.paper_run_days(env.con, env.scope) == []


def test_daily_skips_sealed_days_quietly(tmp_path, monkeypatch):
    from conftest import Env
    from trading_agent import live_window as LW
    nxt = sessions.next_trading_day(D1)
    monkeypatch.setattr(LW, "load", lambda root: LW.parse({"sources": ["webull"], "from_day": {"ES": nxt.isoformat()}}))
    e = Env(tmp_path, dev_visible_through=sessions.prev_trading_day(D1))
    try:
        r = DY.run_daily(e.settings, e.con, OCFG, registry=TR.REG, release_cfg=TRL.CFG, manual_cfg=M.MAN,
                         day=D1.isoformat(), now=sessions.rth_bounds(D1)[1] + 3600, **KW)
        assert r["scopes"][0]["status"] == "SEALED_DAY"
        assert e.con.execute("SELECT COUNT(*) FROM alerts WHERE kind='INCOMPLETE_DAY'").fetchone()[0] == 0
    finally:
        e.con.close()


def test_daily_seals_and_reviews_live_window_day(tmp_path, monkeypatch):
    """放行的实时行情日（截止日之后）可以封存和复盘；聚合照旧把它们排除在外。"""
    from conftest import Env
    from trading_agent import live_window as LW
    monkeypatch.setattr(LW, "load", lambda root: LW.parse({"sources": ["webull"], "from_day": {"ES": D1.isoformat()}}))
    e = Env(tmp_path, dev_visible_through=sessions.prev_trading_day(D1))
    try:
        rows = ER.scripted_day(source="webull")
        for r in rows:
            r["fixture"] = False
        H.write_csv(e.root / "live.csv", rows)
        e.ingest()
        assert e.con.execute("SELECT COUNT(*) FROM bars WHERE source='webull' AND fixture=0").fetchone()[0] > 0
        r = DY.run_daily(e.settings, e.con, OCFG, registry=TR.REG, release_cfg=TRL.CFG, manual_cfg=M.MAN,
                         now=sessions.rth_bounds(D1)[1] + 3600, **KW)
        s = r["scopes"][0]
        assert s["status"] == "DONE" and s["review"] not in ("NO_ACTIVITY",) and not str(s["review"]).startswith("ERROR"), s
        assert r["manual_review"] == "NO_ACTIVITY"
        sid = f"{paper_run_id(e.scope)}:{D1}"
        assert e.con.execute("SELECT status FROM sessions WHERE session_id=?", (sid,)).fetchone()[0] == "SEALED"
        from trading_agent.evolve.aggregate import window
        w = window(e.settings, e.con, str(e.scope), paper_run_id(e.scope), 0)
        assert w["sessions"] == [] and w["excluded"].get("AFTER_DEV_CUTOFF") == 1
    finally:
        e.con.close()
