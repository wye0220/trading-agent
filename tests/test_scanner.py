import json
from datetime import date

import pytest

import helpers as H
import manual_helpers as M
import test_engine_runner as T
from trading_agent import scanner, sessions
from trading_agent.engine import runner as RN
from trading_agent.manual.common import paper_run_id

D1 = T.D1
KW = dict(risk_cfg=T.RISK, engine_cfg=T.ENG)


def live(env, i, lag=5):
    return scanner.live_scan(env.settings, env.con, env.scope, T.PA, now_utc=T.bar_end(i) + lag, **KW)


def hist(env, i, **kw):
    return scanner.historical_scan(env.settings, env.con, env.scope, T.PA, T.bar_end(i) + 5, **KW, **kw)


# ---------------------------------------------------------------- 6.1 盘中
def test_live_scan_queues_paper_entry(env):
    T.load(env, T.scripted_day())
    assert live(env, 4)["signal_status"] == "WAIT"
    r = live(env, 5)
    assert (r["signal_status"], r["risk_status"], r["paper_action"]) == ("SETUP_VALID", "PASS", "QUEUE_ENTRY")
    assert r["run_id"] == paper_run_id(env.scope) and r["order_id"] and r["strategy_ran"] is True
    assert r["contract_id"] == "ESZ2025" and r["session"] == "RTH" and r["snapshot_id"].startswith("snap_")
    assert r["latest_bar"]["available_at_utc"] == sessions.iso(T.bar_end(5) + 5)
    assert r["version_tuple"]["engine_version"] and r["decision_metadata"]["label"] == "H1"
    assert r["working_order"]["trigger_ticks"] == 20041 and r["paper_position"] is None
    run = RN.get_run(env.con, r["run_id"])
    assert (run["run_mode"], run["risk_domain"]) == ("paper", f"paper:{r['run_id']}")
    after = live(env, 6)
    assert after["paper_action"] == "MANAGE_ONLY" and after["paper_position"]["entry_ticks"] == 20041
    assert after["working_order"] is None and "持仓：纸面多仓 1 手，成交 5010.25" in after["report"]


def test_live_scan_is_repeatable(env):
    T.load(env, T.scripted_day())
    live(env, 4)
    a = live(env, 5)
    n = lambda t: env.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]       # noqa: E731
    before = (n("paper_orders"), n("scan_events"))
    b = live(env, 5, lag=40)
    assert (n("paper_orders"), n("scan_events")) == before
    assert (b["signal_status"], b["order_id"]) == (a["signal_status"], a["order_id"])


def test_live_scan_data_error_makes_no_new_orders(env):
    rows = [r for r in T.scripted_day() if r["bar_start_utc"] != sessions.iso(T.bar_end(3) - 300)]    # 缺第 3 根
    T.load(env, rows)
    r = live(env, 5)
    assert r["signal_status"] == "DATA_ERROR" and r["order_id"] is None
    assert [p["category"] for p in r["problems"]] == ["UNEXPECTED_GAP"]
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
    assert "决策：DATA_ERROR，不产生新信号" in r["report"] and "[DATA_ERROR]" in r["report"].splitlines()[0]
    st = env.con.execute("SELECT blocked, reason FROM scope_data_status WHERE risk_domain=?",
                         (f"paper:{paper_run_id(env.scope)}",)).fetchone()
    assert tuple(st) == (1, "UNEXPECTED_GAP")


def test_live_scan_keeps_managing_position_during_data_error(env):
    rows = [r for r in T.scripted_day() if r["bar_start_utc"] != sessions.iso(T.bar_end(8) - 300)]    # 持仓期间之后缺一根
    T.load(env, rows)
    for i in range(4, 8):
        live(env, i)
    assert env.con.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1      # H1 成交并在第 7 根止损
    r = live(env, 9)
    assert r["signal_status"] == "DATA_ERROR" and r["order_id"] is None


def test_live_scan_blocked_by_freeze(env):
    T.load(env, T.scripted_day())
    r4 = live(env, 4)
    RN.set_freeze(env.con, env.scope, RN.get_run(env.con, r4["run_id"])["risk_domain"], frozen=True, cause="MANUAL",
                  reason="交易员冻结")
    r = live(env, 5)
    assert (r["signal_status"], r["risk_status"], r["block_reason"], r["order_id"]) == (
        "SETUP_VALID", "BLOCKED", "FROZEN:MANUAL", None)
    assert "[SETUP_VALID·BLOCKED]" in r["report"] and "但没有挂单。原因：FROZEN:MANUAL" in r["report"]


def test_wait_points_to_next_confirmation(env):
    T.load(env, T.scripted_day())
    r = live(env, 3)
    assert r["signal_status"] == "WAIT" and r["decision_metadata"]["next_confirmation"]
    assert "下一确认点：等待" in r["report"]


# ---------------------------------------------------------------- 6.2 历史
def test_historical_scan_on_fixture_runs_strategy(env):
    T.load(env, T.scripted_day())
    r = hist(env, 5)
    assert r["signal_status"] == "SETUP_VALID" and r["strategy_ran"] and r["decision_metadata"]["label"] == "H1"
    assert r["run_id"].startswith("hscan_") and RN.get_run(env.con, r["run_id"])["run_mode"] == "fixture"
    again = hist(env, 5)                                   # 重复调用：结果相同，不重复建运行
    assert (again["run_id"], again["signal_status"], again["order_id"]) == (r["run_id"], r["signal_status"], r["order_id"])
    assert env.con.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0] == 1
    assert env.con.execute("SELECT COUNT(*) FROM engine_runs WHERE run_id LIKE 'paper:%'").fetchone()[0] == 0


def test_historical_scan_protects_real_data(env):
    T.load(env, [dict(r, fixture="") for r in T.scripted_day()], name="real_day.csv")
    r = hist(env, 5)
    assert r["strategy_ran"] is False and r["decision_metadata"] is None and r["signal_status"] == "WAIT"
    assert "开发可见截止日" in r["strategy_note"] and "策略没有运行" in r["report"]
    assert r["snapshot_id"].startswith("snap_") and r["fixture"] is False
    assert hist(env, 5, allow_real_data=True)["strategy_ran"] is False          # 允许了，但没有设截止日
    assert env.con.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0] == 0


def test_historical_scan_real_data_with_cutoff(tmp_path):
    from conftest import Env
    e = Env(tmp_path, dev_visible_through=D1)
    H.write_csv(e.root / "real_day.csv", [dict(r, fixture="") for r in T.scripted_day()])
    e.ingest()
    f = lambda **kw: scanner.historical_scan(e.settings, e.con, e.scope, T.PA, T.bar_end(5) + 5, **KW, **kw)   # noqa: E731
    assert f()["strategy_ran"] is False
    r = f(allow_real_data=True)
    assert r["strategy_ran"] and r["signal_status"] == "SETUP_VALID"
    assert RN.get_run(e.con, r["run_id"])["run_mode"] == "historical_replay"
    e.con.close()


def test_historical_scan_without_data(env):
    r = hist(env, 5)
    assert r["signal_status"] == "DATA_ERROR" and r["strategy_ran"] is False and "没有数据" in r["strategy_note"]


# ---------------------------------------------------------------- 6.3 短报
def test_report_structure(env):
    T.load(env, T.scripted_day())
    r = hist(env, 5)
    L = r["report"].splitlines()
    assert L[0] == ("[2025-11-25 10:30:05 ET] [pa_brooks:ES:RTH:5m] [ESZ2025] [SETUP_VALID] "
                    "[pa_brooks@v1.0 / fill-v1.0] [FIXTURE]")
    assert [x.split("：")[0] for x in L[1:6]] == ["事实", "决策", "持仓", "纪律", "数据"]
    assert L[-1] == scanner.DISCLAIMER and len(L) == 7
    assert "策略结构：cycle=BULL_SPIKE" in L[1] and "htf_direction=UNKNOWN" in L[1]
    assert "触发 5010.25，限价 5010.50，止损 5006.75" in L[2] and "有效至 10:35 ET" in L[2]
    assert "挂单 ARMED：触发 5010.25" in L[3] and "今日纸面止损 0 次" in L[4]
    assert "资讯窗口：不可用（未配置）" in L[5] and "到达 10:30:05 ET（估计）" in L[5]


def test_scanner_does_not_hardcode_strategy_terms():
    text = (H.PROJECT / "src" / "trading_agent" / "scanner.py").read_text(encoding="utf-8")
    import re
    assert not re.search(r"spike|pullback|\bH[123]\b|pa_brooks|brooks", text, re.I)


def test_news_windows_in_report(env, monkeypatch):
    T.load(env, T.scripted_day())
    t = T.bar_end(5)
    monkeypatch.setattr(scanner, "load_news_windows", lambda root: [
        {"label": "FOMC 决议", "start_utc": t - 600, "end_utc": t + 600},
        {"label": "记者会", "start_utc": t + 1800, "end_utc": t + 5400}])
    r = hist(env, 5)
    assert r["news_windows"]["available"] and [w["label"] for w in r["news_windows"]["active"]] == ["FOMC 决议"]
    assert "资讯窗口：正在进行 FOMC 决议 10:20–10:40 ET；4 小时内 记者会 11:00–12:00 ET" in r["report"]
    monkeypatch.setattr(scanner, "load_news_windows", lambda root: [{"label": "CPI", "start_utc": t + 90000, "end_utc": t + 93600}])
    assert "资讯窗口：当前不在窗口内" in live(env, 5)["report"]


# ---------------------------------------------------------------- 6.4 文档里的样例
def samples(env):
    """生成 docs/scan-plan-watch.md 里的五段样例输出。"""
    T.load(env, T.scripted_day())
    out = {"WAIT": hist(env, 3)["report"]}
    live(env, 4)
    out["PAPER_ENTRY"] = live(env, 5)["report"]
    return out


def test_doc_samples_match_actual_output(tmp_path):
    from conftest import Env
    doc = (H.PROJECT / "docs" / "scan-plan-watch.md").read_text(encoding="utf-8")
    e = Env(tmp_path / "a")
    s = samples(e)
    assert s["WAIT"] in doc and s["PAPER_ENTRY"] in doc
    e.con.close()

    e = Env(tmp_path / "b")                                # BLOCKED
    T.load(e, T.scripted_day())
    r4 = live(e, 4)
    RN.set_freeze(e.con, e.scope, RN.get_run(e.con, r4["run_id"])["risk_domain"], frozen=True, cause="MANUAL")
    assert live(e, 5)["report"] in doc
    e.con.close()

    e = Env(tmp_path / "c")                                # DEVIATES 预检
    now = M.paper_state(e, 4)
    c = M.mkplan(e, "ES S 5007 5011 4999 1 fade", now=now, plan_id="plan_example")["check"]
    assert c["conclusion"] == "DEVIATES" and c["summary"] in doc
    e.con.close()

    e = Env(tmp_path / "d")                                # NAKED_POSITION 提醒
    t = T.bar_end(5)
    M.snapshot(e, t, [M.pos(1)])
    M.poll(e, t + 1)
    M.snapshot(e, t + 61, [M.pos(1)])
    a = [x for x in M.poll(e, t + 62)["alerts"] if x["rule"] == "NAKED_POSITION"][0]
    assert a["message"] in doc
    e.con.close()
