import copy
import json
import re

import pytest
import yaml

import helpers as H
import manual_helpers as M
import test_engine_runner as T
from trading_agent import db
from trading_agent.config import ConfigError
from trading_agent.engine import risk as R
from trading_agent.manual import config as MC, plans

NOW = T.bar_end(5) + 10                                   # 脚本日 10:30:10 ET


# ---------------------------------------------------------------- 1.2 配置
def test_manual_config_defaults_and_validation():
    c = MC.load_manual(H.TEST_PROJECT)
    assert (c.default_account_id, c.plan_ttl_minutes, c.cooldown_minutes, c.quiet_window_minutes) == (None, 30, 15, 15)
    assert (c.plan_coverage_threshold, c.stop_tolerance_ticks, c.daily_loss_warn_fraction) == (0.8, 2, 0.8)
    assert all(c.rules[r] for r in MC.RULES) and c.strategy_state_max_age_bars == 2
    raw = yaml.safe_load((H.TEST_PROJECT / "config" / "manual.yaml").read_text(encoding="utf-8"))
    for key, bad in (("plan_ttl_minutes", 0), ("plan_coverage_threshold", 1.5), ("cooldown_minutes", -1),
                     ("rules", {"NOPE": True}), ("rules", {"NAKED_POSITION": "yes"})):
        r = copy.deepcopy(raw)
        r[key] = bad
        with pytest.raises(ConfigError):
            MC.parse_manual(r)
    r = copy.deepcopy(raw)
    r["default_account_id"] = "U123"
    r["rules"] = {"COUNTERTREND_ADD": False}
    c2 = MC.parse_manual(r)
    assert c2.default_account_id == "U123" and c2.rules["COUNTERTREND_ADD"] is False and c2.rules["NAKED_POSITION"]


def test_news_windows_config(tmp_path):
    assert MC.load_news_windows(H.TEST_PROJECT) == []
    (tmp_path / "config").mkdir()
    f = tmp_path / "config" / "news_windows.yaml"
    f.write_text('windows:\n  - {label: FOMC, start_utc: "2025-11-25T19:00:00Z", end_utc: "2025-11-25T20:00:00Z"}\n')
    (w,) = MC.load_news_windows(tmp_path)
    assert w["label"] == "FOMC" and w["end_utc"] - w["start_utc"] == 3600
    f.write_text('windows:\n  - {label: X, start_utc: "2025-11-25T20:00:00Z", end_utc: "2025-11-25T19:00:00Z"}\n')
    with pytest.raises(ConfigError):
        MC.load_news_windows(tmp_path)


# ---------------------------------------------------------------- 2.1 快捷语法
def test_quick_syntax(env):
    p = M.quick(env, "ES L 5000 4995 5010 1 PA Spike H1")
    assert (p.instrument_root, p.contract_id, p.side, p.quantity, p.reason) == ("ES", "ESZ2025", "LONG", 1, "PA Spike H1")
    assert (p.entry_ticks, p.stop_ticks, p.target_ticks) == (20000, 19980, 20040)
    s = M.quick(env, "es s 5000.25 5004 - 2")
    assert (s.side, s.stop_ticks, s.target_ticks, s.quantity, s.reason) == ("SHORT", 20016, None, 2, "")
    assert M.quick(env, "ES L 5000 - - 1").stop_ticks is None


@pytest.mark.parametrize("text,needle", [
    ("ES L 5000", "计划缺少字段：止损、目标、手数"),
    ("ES L 5000.10 4995 5010 1", "入场价不合法"),
    ("CL L 70 69 72 1", "品种未登记"),
    ("ES X 5000 4995 5010 1", "方向只能是 L 或 S"),
    ("ES L 5000 5005 5010 1", "止损在入场的错误一侧"),
    ("ES L 5000 4995 4990 1", "目标在入场的错误一侧"),
    ("ES L 5000 4995 5010 0", "手数应为正整数"),
    ("ES L 5000 4995 5010 abc", "手数应为正整数"),
])
def test_quick_syntax_errors(env, text, needle):
    with pytest.raises(plans.PlanError, match=needle):
        M.quick(env, text)
    assert env.con.execute("SELECT COUNT(*) FROM trader_plans").fetchone()[0] == 0


# ---------------------------------------------------------------- 2.2 登记与状态机
def test_create_is_draft_without_reservation(env):
    r = M.mkplan(env, now=NOW)
    assert r["status"] == "DRAFT" and r["reserved"] is False and r["contract_id"] == "ESZ2025"
    p = plans.get(env.con, r["plan_id"])
    assert (p["status"], p["scope"], p["strategy_family"], p["filled_quantity"]) == ("DRAFT", "pa_brooks:ES:RTH:5m", "pa_brooks", 0)
    assert p["expires_at_utc"] == NOW + 30 * 60 and p["submitted_at_utc"] == NOW
    assert json.loads(p["version_tuple_json"])["strategy_version"] == "v1.0"
    assert env.con.execute("SELECT COUNT(*) FROM risk_reservations").fetchone()[0] == 0
    assert R.account_state(env.con, M.ACCT, "2025-11-25").reserved_usd == 0


def test_audit_trail_and_versioning(env):
    r = M.mkplan(env, now=NOW)
    pid = r["plan_id"]
    assert M.activate(env, pid, now=NOW + 5)["status"] == "ACTIVE"
    assert plans.cancel(env.con, pid, NOW + 9)["status"] == "CANCELLED"
    ev = [(e["from_status"], e["to_status"], e["at_utc"]) for e in env.con.execute(
        "SELECT * FROM plan_events WHERE plan_id=? ORDER BY event_id", (pid,))]
    assert ev == [(None, "DRAFT", NOW), ("DRAFT", "ACTIVE", NOW + 5), ("ACTIVE", "CANCELLED", NOW + 9)]
    assert plans.get(env.con, pid)["version"] == 3
    with pytest.raises(plans.PlanError, match="不能取消"):
        plans.cancel(env.con, pid)
    with pytest.raises(plans.PlanError, match="没有这份计划"):
        plans.get(env.con, "nope")
    stale = dict(plans.get(env.con, pid))
    with pytest.raises(db.StaleVersion):
        plans._transition(env.con, {**stale, "version": 1}, "EXPIRED", "x", NOW + 20)
    env.con.rollback()
    assert plans.was_active_at(env.con, pid, NOW + 6) and not plans.was_active_at(env.con, pid, NOW + 2)
    assert not plans.was_active_at(env.con, pid, NOW + 10)


# ---------------------------------------------------------------- 2.3 预检
def test_precheck_blocked_when_budget_not_approved(env):
    r = M.mkplan(env, now=NOW, risk=M.UNFUNDED)
    c = r["check"]
    assert (c["conclusion"], c["risk_status"], c["risk_reason"]) == ("BLOCKED_BY_RISK", "BLOCKED", "BUDGET_NOT_APPROVED")
    assert "不下单" in c["summary"]


def test_precheck_unknown_alignment_is_deviates(env):
    c = M.mkplan(env, now=NOW)["check"]                   # 没有纸面运行
    assert (c["strategy_alignment"], c["conclusion"], c["risk_status"]) == ("UNKNOWN", "DEVIATES", "PASS")
    assert c["setup_id"] is None and c["discipline_flags"] == []


def test_precheck_no_stop(env):
    c = M.mkplan(env, "ES L 5000 - 5010 1", now=NOW)["check"]
    assert "NO_STOP" in c["discipline_flags"] and (c["risk_status"], c["risk_reason"]) == ("BLOCKED", "NO_STOP")


def test_precheck_stale_account_snapshot(env):
    c = M.mkplan(env, now=NOW, fetched=False)["check"]
    assert (c["conclusion"], c["risk_reason"]) == ("BLOCKED_BY_RISK", "RISK_UNKNOWN_OR_STALE")


def test_precheck_risk_over_budget(env):
    c = M.mkplan(env, "ES L 5000 4996 5008 3", now=NOW)["check"]      # 规则只允许 2 手
    assert c["conclusion"] == "BLOCKED_BY_RISK" and "RISK_OVER_BUDGET" in c["discipline_flags"]


def test_precheck_aligned_with_active_signal(env):
    now = M.paper_state(env, 5)                           # H1 刚出现：触发 5010.25，止损 5006.75
    r = M.mkplan(env, "ES L 5010.25 5006.75 5017.25 1 H1", now=now)
    c = r["check"]
    assert (c["conclusion"], c["strategy_alignment"], c["risk_status"]) == ("ALIGNED", "ALIGNED", "PASS")
    assert c["setup_id"] and c["discipline_flags"] == []
    assert plans.get(env.con, r["plan_id"])["setup_id"] == c["setup_id"]


def test_precheck_countertrend(env):
    now = M.paper_state(env, 4)                           # 牛 Spike 的回调中
    c = M.mkplan(env, "ES S 5007 5011 4999 1 fade", now=now)["check"]
    assert (c["conclusion"], c["strategy_alignment"]) == ("DEVIATES", "DEVIATES")
    assert c["discipline_flags"] == ["COUNTERTREND_BEFORE_CONFIRMATION"]
    assert any("spike_direction=BULL" in x for x in c["observable_conditions"])


def test_precheck_countertrend_long_in_bear_spike(env):
    now = M.paper_state(env, 4, mirror=True)
    c = M.mkplan(env, "ES L 4993 4989 5001 1 bottom fishing", now=now)["check"]
    assert c["conclusion"] == "DEVIATES" and "COUNTERTREND_BEFORE_CONFIRMATION" in c["discipline_flags"]


def test_precheck_strategy_state_too_old(env):
    now = M.paper_state(env, 4)
    c = M.mkplan(env, "ES S 5007 5011 4999 1", now=now + 20 * 60)["check"]      # 状态落后四根 K
    assert c["strategy_alignment"] == "UNKNOWN"


# ---------------------------------------------------------------- 2.4 无新形态的重试
def stopped_out(env, at, side="BUY"):
    exit_side = "SELL" if side == "BUY" else "BUY"
    M.do_import(env, [M.trade(at - 600, side, 5000, fill="o1", order="E1"),
                      M.trade(at, exit_side, 4996 if side == "BUY" else 5004, fill="c1", order="S1")],
                [M.oevent(at - 590, "S1", "SUBMITTED", "STOP", 4996, side=exit_side),
                 M.oevent(at, "S1", "FILLED", "STOP", 4996, side=exit_side)])


def test_retry_without_new_setup(env):
    stopped_out(env, NOW - 60)
    c = M.mkplan(env, "ES L 5000 4996 5008 1 again", now=NOW)["check"]
    assert "RETRY_WITHOUT_NEW_SETUP" in c["discipline_flags"]
    other = M.mkplan(env, "ES S 5000 5004 4992 1 flip", now=NOW)["check"]          # 换方向不算
    assert "RETRY_WITHOUT_NEW_SETUP" not in other["discipline_flags"]


def test_retry_not_flagged_after_profit(env):
    stopped_out(env, NOW - 300)
    M.do_import(env, [M.trade(NOW - 200, "BUY", 5000, fill="o2", order="E2"),
                      M.trade(NOW - 100, "SELL", 5008, fill="c2", order="T2")],
                [M.oevent(NOW - 100, "T2", "FILLED", "LIMIT", 5008)], name="t2")
    c = M.mkplan(env, "ES L 5000 4996 5008 1", now=NOW)["check"]
    assert "RETRY_WITHOUT_NEW_SETUP" not in c["discipline_flags"]


# ---------------------------------------------------------------- 2.5 激活、取消、过期
def test_activate_reserves(env):
    pid = M.mkplan(env, now=NOW)["plan_id"]
    r = M.activate(env, pid, now=NOW + 5)
    assert r["status"] == "ACTIVE" and r["reserved"] is True
    st = R.account_state(env.con, M.ACCT, "2025-11-25")
    assert st.reserved_usd == R.planned_risk_per_contract(env.settings.instrument("ES"), 16, M.FUNDED)
    assert plans.get(env.con, pid)["activated_at_utc"] == NOW + 5
    with pytest.raises(plans.PlanError, match="只有 DRAFT 的计划能激活"):
        M.activate(env, pid, now=NOW + 6)


def test_activate_blocked_keeps_draft(env):
    import test_risk as TR
    tight = TR.funded(account__daily_loss_limit_usd=300)             # 只够一份计划
    a = M.mkplan(env, now=NOW, risk=tight)["plan_id"]
    b = M.mkplan(env, now=NOW, risk=tight)["plan_id"]
    assert M.activate(env, a, now=NOW + 1, risk=tight)["status"] == "ACTIVE"
    r = M.activate(env, b, now=NOW + 2, risk=tight)
    assert (r["status"], r["reserved"], r["check"]["conclusion"]) == ("DRAFT", False, "BLOCKED_BY_RISK")
    assert "DAILY_LIMIT_HIT" in r["check"]["discipline_flags"]
    assert plans.get(env.con, b)["status"] == "DRAFT"
    assert env.con.execute("SELECT COUNT(*) FROM risk_reservations WHERE status='RESERVED'").fetchone()[0] == 1


def test_expire_and_cancel_release_reservation(env):
    a = M.mkplan(env, now=NOW)["plan_id"]
    b = M.mkplan(env, now=NOW)["plan_id"]
    M.activate(env, a, now=NOW + 1)
    M.activate(env, b, now=NOW + 2)
    assert R.account_state(env.con, M.ACCT, "2025-11-25").reserved_usd == 435
    plans.cancel(env.con, b, NOW + 3)
    assert R.account_state(env.con, M.ACCT, "2025-11-25").reserved_usd == R.planned_risk_per_contract(
        env.settings.instrument("ES"), 16, M.FUNDED)
    assert plans.expire_due(env.con, NOW + 29 * 60) == []
    assert plans.expire_due(env.con, NOW + 31 * 60) == [a]
    assert plans.get(env.con, a)["status"] == "EXPIRED"
    assert R.account_state(env.con, M.ACCT, "2025-11-25").reserved_usd == 0
    rows = plans.list_plans(env.con, M.ACCT)
    assert {r["plan_id"]: r["status"] for r in rows} == {a: "EXPIRED", b: "CANCELLED"}
    assert plans.list_plans(env.con, M.ACCT, only_open=True) == []


def test_manual_code_never_places_orders():
    src = H.PROJECT / "src" / "trading_agent" / "manual"
    banned = re.compile(r"place_order|submit_order|replace_order|cancel_order|modify_order|webull\.trade|TradeClient|"
                        r"\.post\(|requests\.|urllib|http\.client|socket", re.I)
    files = sorted(src.glob("*.py")) + [H.PROJECT / "src" / "trading_agent" / "scanner.py"]
    assert len(files) >= 8
    for f in files:
        assert not banned.search(f.read_text(encoding="utf-8")), f
