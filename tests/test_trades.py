import json
from decimal import Decimal

import pytest

import helpers as H
import manual_helpers as M
import test_engine_runner as T
from trading_agent.engine import risk as R
from trading_agent.manual import discipline as DC, plans, trades as TM

NOW = T.bar_end(5) + 10
DAY = "2025-11-25"


def rows(env, sql, *args):
    return [dict(r) for r in env.con.execute(sql, args)]


def parts(env):
    return rows(env, "SELECT * FROM manual_trades WHERE quarantined=0 ORDER BY executed_at_utc, effect")


def events(env, type_=None):
    q = "SELECT * FROM discipline_events" + (" WHERE type=?" if type_ else "") + " ORDER BY action_time_utc"
    return rows(env, q, *([type_] if type_ else []))


def active_plan(env, text="ES L 5000 4995 5010 1 plan", at=NOW - 900):
    pid = M.mkplan(env, text, now=at)["plan_id"]
    assert M.activate(env, pid, now=at + 5)["status"] == "ACTIVE"
    return pid


# ---------------------------------------------------------------- 3.1 读取、幂等、隔离
def test_import_ok_and_idempotent(env):
    t = [M.trade(NOW - 600, "BUY", 5000, fill="f1", order="o1"), M.trade(NOW - 60, "SELL", 5004, fill="f2", order="o2")]
    a = M.do_import(env, t)["trades"]
    assert (a["imported"], a["skipped"], a["quarantined"], a["bad_rows"]) == (2, 0, 0, 0)
    p = parts(env)
    assert [(x["effect"], x["side"], x["quantity"], x["price_ticks"]) for x in p] == [("OPEN", "LONG", 1, 20000), ("CLOSE", "LONG", 1, 20016)]
    assert p[0]["contract_id"] == "ESZ2025" and p[0]["source"] == "broker_csv" and p[0]["imported_at_utc"] > 0
    assert p[0]["match_rule"] == "BROKER_FILL_ID" and p[0]["trading_day"] == DAY
    stops = R.account_state(env.con, M.ACCT, DAY).stops
    b = M.do_import(env, t)["trades"]
    assert (b["imported"], b["skipped"]) == (0, 2) and len(parts(env)) == 2
    assert R.account_state(env.con, M.ACCT, DAY).stops == stops


def test_missing_column_rejects_file(env):
    cols = [c for c in M.TRADE_COLS if c != "executed_at_utc"]
    p = H.write_csv(env.tmp / "bad.csv", [M.trade(NOW, "BUY", 5000)], columns=cols)
    with pytest.raises(TM.ImportError_, match="缺少必需列：executed_at_utc"):
        TM.import_trades(env.settings, env.con, p, risk_cfg=M.FUNDED, manual_cfg=M.MAN)
    assert env.con.execute("SELECT COUNT(*) FROM manual_trades").fetchone()[0] == 0


def test_bad_rows_are_counted_not_imported(env):
    r = M.do_import(env, [M.trade(NOW, "BUY", 5000.1, fill="a"), M.trade(NOW, "HOLD", 5000, fill="b"),
                          M.trade(NOW, "BUY", 5000, qty=0, fill="c"), M.trade(NOW, "BUY", 5000, fill="d")])["trades"]
    assert (r["imported"], r["bad_rows"]) == (1, 3) and "第 2 行" in r["first_error"]


def test_identical_rows_without_fill_id_are_quarantined(env):
    same = M.trade(NOW - 60, "BUY", 5000)
    r = M.do_import(env, [same, dict(same), M.trade(NOW - 30, "BUY", 5001)])["trades"]
    assert (r["imported"], r["quarantined"]) == (1, 2)
    q = rows(env, "SELECT * FROM manual_trades WHERE quarantined=1")
    assert len(q) == 2 and all(x["effect"] is None for x in q)
    (ok,) = parts(env)
    assert ok["match_rule"] == "SYNTHETIC_KEY" and ok["price_ticks"] == 20004
    k = DC.session_kpis(env.con, M.MAN, M.ACCT, DAY)
    assert k["opens"] == 1 and k["quarantined_trades"] == 2          # 隔离的不计入开仓数
    again = M.do_import(env, [M.trade(NOW - 30, "BUY", 5001)], name="again")["trades"]
    assert again["skipped"] == 1                                       # 合成键也幂等


# ---------------------------------------------------------------- 3.2 开平与盈亏
def test_reversal_is_split(env):
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="f1"), M.trade(NOW - 60, "SELL", 5002, qty=2, fill="f2")])
    p = parts(env)
    assert [(x["effect"], x["side"], x["quantity"]) for x in p] == [("OPEN", "LONG", 1), ("CLOSE", "LONG", 1), ("OPEN", "SHORT", 1)]
    assert p[1]["fill_key"] == p[2]["fill_key"] and p[1]["broker_fill_id"] == "f2" and p[2]["broker_fill_id"] is None


def test_fifo_realized_pnl(env):
    M.do_import(env, [M.trade(NOW - 900, "BUY", 5000, fill="a"), M.trade(NOW - 800, "BUY", 5004, fill="b"),
                      M.trade(NOW - 100, "SELL", 5006, qty=1, fill="c"), M.trade(NOW - 50, "SELL", 5001, qty=1, fill="d")])
    closes = [x for x in parts(env) if x["effect"] == "CLOSE"]
    # 先进先出：第一笔平仓对 5000 的那一手（+6 点 = 300 美元，减 5 美元费用），第二笔对 5004 的（-3 点 = -150，减 5）
    assert [Decimal(x["realized_usd"]) for x in closes] == [Decimal(295), Decimal(-155)]
    assert [x["entry_ticks"] for x in closes] == [20000, 20016]
    assert R.account_state(env.con, M.ACCT, DAY).realized_loss_usd == Decimal(-140)
    short = [M.trade(NOW - 40, "SELL", 5010, fill="e", account="acct2"), M.trade(NOW - 20, "BUY", 5012, fill="f", account="acct2")]
    M.do_import(env, short, name="s")
    c = rows(env, "SELECT * FROM manual_trades WHERE account_id='acct2' AND effect='CLOSE'")[0]
    assert (c["side"], Decimal(c["realized_usd"])) == ("SHORT", Decimal(-105))


# ---------------------------------------------------------------- 3.3 / 3.4 订单事件与账户风险
def test_order_events_import(env):
    ev = [M.oevent(NOW - 500, "S1", "SUBMITTED", "STOP", 4995), M.oevent(NOW - 300, "S1", "MODIFIED", "STOP", 4990)]
    a = M.do_import(env, events=ev)["events"]
    assert (a["imported"], a["skipped"], a["bad_rows"]) == (2, 0, 0)
    m = rows(env, "SELECT * FROM manual_order_events WHERE event_type='MODIFIED'")[0]
    assert (m["order_kind"], m["price_ticks"], m["event_time_utc"], m["contract_id"]) == ("STOP", 19960, NOW - 300, "ESZ2025")
    assert M.do_import(env, events=ev)["events"]["skipped"] == 2
    bad = M.do_import(env, events=[M.oevent(NOW, "S2", "EXPLODED", "STOP", 4990)], name="b")["events"]
    assert bad["bad_rows"] == 1
    assert TM.order_kind(env.con, M.ACCT, "S1") == "STOP" and TM.order_kind(env.con, M.ACCT, "zzz") == "UNKNOWN"
    assert TM.order_kind(env.con, M.ACCT, None) == "UNKNOWN"


def test_stop_exit_counts_toward_account_stops(env):
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="o", order="E1"), M.trade(NOW - 60, "SELL", 4995, fill="c", order="S1")],
                [M.oevent(NOW - 590, "S1", "SUBMITTED", "STOP", 4995), M.oevent(NOW - 60, "S1", "FILLED", "STOP", 4995)])
    st = R.account_state(env.con, M.ACCT, DAY)
    assert st.stops == 1 and st.realized_loss_usd == Decimal(255)
    assert [x["exit_kind"] for x in parts(env) if x["effect"] == "CLOSE"] == ["STOP"]


def test_loss_without_order_events_is_not_a_stop(env):
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="o"), M.trade(NOW - 60, "SELL", 4995, fill="c", order="X")])
    st = R.account_state(env.con, M.ACCT, DAY)
    assert st.stops == 0 and st.realized_loss_usd == Decimal(255)
    assert [x["exit_kind"] for x in parts(env) if x["effect"] == "CLOSE"] == ["UNKNOWN"]


# ---------------------------------------------------------------- 3.5 计划关联
def test_linked_when_plan_id_and_active_before(env):
    pid = active_plan(env)
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="f1", plan=pid)])
    (t,) = parts(env)
    assert (t["plan_link_status"], t["plan_id"]) == ("LINKED", pid)
    p = plans.get(env.con, pid)
    assert (p["status"], p["filled_quantity"]) == ("FILLED", 1)
    assert R.account_state(env.con, M.ACCT, DAY).reserved_usd == 0          # 满额后预留释放
    assert events(env) == []


def test_time_match_only_is_unknown_with_suggestion(env):
    pid = active_plan(env)
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="f1")])
    (t,) = parts(env)
    assert (t["plan_link_status"], t["plan_id"], t["suggested_plan_id"]) == ("UNKNOWN", None, pid)
    assert plans.get(env.con, pid)["status"] == "ACTIVE" and events(env, "UNPLANNED") == []


def test_no_plan_is_unplanned(env):
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="f1")])
    (t,) = parts(env)
    assert t["plan_link_status"] == "UNPLANNED"
    (e,) = events(env, "UNPLANNED")
    assert (e["trade_id"], e["plan_adherence"], e["strategy_alignment"], e["action_time_utc"]) == (
        t["trade_id"], "VIOLATION", "UNKNOWN", NOW - 600)


def test_backdated_plan_does_not_count(env):
    pid = active_plan(env, at=NOW - 100)                    # 计划在成交之后才提交和激活
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="f1", plan=pid)])
    (t,) = parts(env)
    assert t["plan_link_status"] == "UNPLANNED" and "事后补录" in t["link_note"]
    assert plans.get(env.con, pid)["filled_quantity"] == 0 and len(events(env, "UNPLANNED")) == 1


def test_plan_mismatch_and_missing(env):
    pid = active_plan(env, "ES S 5000 5005 4990 1 short plan")
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="f1", plan=pid), M.trade(NOW - 500, "SELL", 5001, fill="f2"),
                      M.trade(NOW - 400, "BUY", 5000, fill="f3", plan="plan_missing")])
    notes = [x["link_note"] for x in parts(env) if x["effect"] == "OPEN"]
    assert "方向与成交不符" in notes[0] and "不存在" in notes[1]


def test_partial_fills(env):
    pid = active_plan(env, "ES L 5000 4998 5010 2 two lots")
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="f1", plan=pid)])
    assert plans.get(env.con, pid)["status"] == "PARTIALLY_FILLED"
    assert R.account_state(env.con, M.ACCT, DAY).reserved_usd > 0
    M.do_import(env, [M.trade(NOW - 500, "BUY", 5000.25, fill="f2", plan=pid)], name="t2")
    p = plans.get(env.con, pid)
    assert (p["status"], p["filled_quantity"]) == ("FILLED", 2)
    assert R.account_state(env.con, M.ACCT, DAY).reserved_usd == 0
    tos = [e["to_status"] for e in env.con.execute("SELECT to_status FROM plan_events WHERE plan_id=? ORDER BY event_id", (pid,))]
    assert tos == ["DRAFT", "ACTIVE", "PARTIALLY_FILLED", "FILLED"]


# ---------------------------------------------------------------- 4.1 纪律事件
def round_trip(env, exit_price, exit_kind=None, *, plan_text="ES L 5000 4995 5010 1 plan", extra_events=(), with_plan=True):
    pid = active_plan(env, plan_text) if with_plan else None
    ev = list(extra_events)
    if exit_kind:
        ev.append(M.oevent(NOW - 60, "X1", "FILLED", exit_kind, exit_price))
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="in", order="E1", plan=pid),
                      M.trade(NOW - 60, "SELL", exit_price, fill="out", order="X1")], ev or None)
    close = [x for x in parts(env) if x["effect"] == "CLOSE"][0]
    return pid, close


def test_planned_stop_is_compliant(env):
    pid, c = round_trip(env, 4995, "STOP")
    assert (c["plan_adherence"], c["exit_kind"], c["plan_id"]) == ("COMPLIANT", "STOP", pid) and events(env) == []


def test_stop_order_gapped_is_not_delay(env):
    _, c = round_trip(env, 4990, "STOP")                    # 止损单跳空成交在 4990
    assert c["plan_adherence"] == "COMPLIANT" and events(env, "STOP_DELAYED") == []


def test_market_exit_beyond_stop_is_delay(env):
    pid, c = round_trip(env, 4990, "MARKET")
    (e,) = events(env, "STOP_DELAYED")
    ev = json.loads(e["evidence_json"])
    assert (e["plan_id"], e["plan_adherence"], ev["worse_by_ticks"], ev["exit_kind"]) == (pid, "VIOLATION", 20, "MARKET")
    assert c["plan_adherence"] == "VIOLATION"


def test_market_exit_within_tolerance_is_not_delay(env):
    _, c = round_trip(env, 4994.5, "MARKET")                # 比止损差 2 tick，在容差内
    assert c["plan_adherence"] == "COMPLIANT" and events(env) == []


def test_early_exit_even_when_profitable(env):
    _, c = round_trip(env, 5004, "MARKET")                  # 盈利，但没到目标 5010
    (e,) = events(env, "EARLY_EXIT")
    assert e["plan_adherence"] == "VIOLATION" and c["plan_adherence"] == "VIOLATION" and Decimal(c["realized_usd"]) > 0


def test_target_limit_fill_is_compliant(env):
    _, c = round_trip(env, 5010, "LIMIT")
    assert c["plan_adherence"] == "COMPLIANT" and events(env) == []


def test_stop_widened(env):
    moved = [M.oevent(NOW - 580, "S9", "SUBMITTED", "STOP", 4995), M.oevent(NOW - 300, "S9", "MODIFIED", "STOP", 4990)]
    pid, c = round_trip(env, 4990, None, extra_events=moved + [M.oevent(NOW - 60, "X1", "FILLED", "STOP", 4990)])
    (e,) = events(env, "STOP_WIDENED")
    ev = json.loads(e["evidence_json"])
    assert (ev["from_ticks"], ev["to_ticks"], ev["at_utc"], e["plan_adherence"]) == (19980, 19960, NOW - 300, "VIOLATION")
    assert c["plan_adherence"] == "VIOLATION" and events(env, "STOP_DELAYED") == []     # 止损单成交，不另记延迟


def test_stop_tightened_is_fine(env):
    moved = [M.oevent(NOW - 580, "S9", "SUBMITTED", "STOP", 4995), M.oevent(NOW - 300, "S9", "MODIFIED", "STOP", 4998)]
    _, c = round_trip(env, 4998, None, extra_events=moved + [M.oevent(NOW - 60, "X1", "FILLED", "STOP", 4998)])
    assert events(env) == [] and c["plan_adherence"] == "COMPLIANT"


def test_no_order_events_is_missing_evidence(env):
    pid, c = round_trip(env, 4990)
    (e,) = events(env)
    assert (e["type"], e["plan_adherence"], e["plan_id"]) == ("MISSING_EVIDENCE", "UNKNOWN", pid)
    assert c["plan_adherence"] == "UNKNOWN"


def test_close_without_plan_is_unknown(env):
    _, c = round_trip(env, 4990, "MARKET", with_plan=False)
    assert c["plan_adherence"] == "UNKNOWN" and c["strategy_alignment"] == "UNKNOWN"
    assert [e["type"] for e in events(env)] == ["UNPLANNED"]


def test_plan_compliant_but_strategy_deviates(env):
    now = M.paper_state(env, 4)                             # 牛 Spike 的回调中，计划做空
    pid = M.mkplan(env, "ES S 5007 5011 4999 1 fade", now=now)["plan_id"]
    assert M.activate(env, pid, now=now + 1)["status"] == "ACTIVE"
    M.do_import(env, [M.trade(now + 60, "SELL", 5007, fill="in", order="E1", plan=pid),
                      M.trade(now + 600, "BUY", 5011, fill="out", order="X1")],
                [M.oevent(now + 600, "X1", "FILLED", "STOP", 5011, side="BUY")])
    close = [x for x in parts(env) if x["effect"] == "CLOSE"][0]
    assert (close["plan_adherence"], close["strategy_alignment"]) == ("COMPLIANT", "DEVIATES")
    (e,) = events(env, "COUNTERTREND_BEFORE_CONFIRMATION")
    assert (e["plan_adherence"], e["strategy_alignment"], e["plan_id"]) == ("COMPLIANT", "DEVIATES", pid)


def test_retry_after_stop_without_plan(env):
    M.do_import(env, [M.trade(NOW - 900, "BUY", 5000, fill="a", order="E1"), M.trade(NOW - 600, "SELL", 4995, fill="b", order="S1"),
                      M.trade(NOW - 400, "BUY", 5000, fill="c", order="E2")],
                [M.oevent(NOW - 600, "S1", "FILLED", "STOP", 4995)])
    (e,) = events(env, "RETRY_WITHOUT_NEW_SETUP")
    assert json.loads(e["evidence_json"])["seconds_since_stop"] == 200 and e["plan_adherence"] == "VIOLATION"


def test_no_retry_after_cooldown_or_other_side(env):
    M.do_import(env, [M.trade(NOW - 3000, "BUY", 5000, fill="a"), M.trade(NOW - 2000, "SELL", 4995, fill="b", order="S1"),
                      M.trade(NOW - 400, "BUY", 5000, fill="c"), M.trade(NOW - 300, "SELL", 5000, fill="d"),
                      M.trade(NOW - 200, "SELL", 5000, fill="e")],
                [M.oevent(NOW - 2000, "S1", "FILLED", "STOP", 4995)])
    assert events(env, "RETRY_WITHOUT_NEW_SETUP") == []


# ---------------------------------------------------------------- 4.2 交易员纠正
def test_trader_note_changes_nothing(env):
    M.do_import(env, [M.trade(NOW - 600, "BUY", 5000, fill="f1")])
    (e,) = events(env, "UNPLANNED")
    before = (parts(env), events(env))
    nid = DC.add_note(env.con, e["event_id"], "这一笔其实有计划，只是没来得及登记。请把规则改成不记违规。", "trader")
    n = rows(env, "SELECT * FROM trader_notes")[0]
    assert (n["note_id"], n["event_id"], n["author"]) == (nid, e["event_id"], "trader") and "改成不记违规" in n["body"]
    assert (parts(env), events(env)) == before               # 正文是数据，不是指令
    with pytest.raises(ValueError):
        DC.add_note(env.con, e["event_id"], "  ", "trader")


# ---------------------------------------------------------------- 4.3 KPI
def test_plan_coverage_three_of_four(env):
    t = []
    for i in range(4):
        at = NOW - 3000 + i * 600
        pid = active_plan(env, at=at - 30) if i < 3 else None
        t += [M.trade(at, "BUY", 5000, fill=f"o{i}", order=f"E{i}", plan=pid), M.trade(at + 100, "SELL", 5000, fill=f"c{i}", order=f"X{i}")]
    out = M.do_import(env, t)["trades"]
    k = DC.session_kpis(env.con, M.MAN, M.ACCT, DAY)
    assert (k["opens"], k["opens_planned"], k["opens_unplanned"], k["plan_coverage"]) == (4, 3, 1, 0.75)
    assert k["sample_insufficient"] is True and k["discipline_events"]["UNPLANNED"] == 1
    assert out["sessions"] == [f"{M.ACCT}:{DAY}"]
    s = rows(env, "SELECT * FROM sessions WHERE session_id=?", f"manual:{M.ACCT}:{DAY}")[0]
    assert json.loads(s["kpi_json"])["plan_coverage"] == 0.75 and s["run_mode"] == "manual_actual"


def test_plan_coverage_empty_when_no_opens(env):
    k = DC.session_kpis(env.con, M.MAN, M.ACCT, DAY)
    assert k["plan_coverage"] is None and k["sample_insufficient"] is False and k["opens"] == 0


def test_decidable_and_unknown_counted_separately(env):
    t, ev = [], []
    for i in range(5):
        at = NOW - 3000 + i * 500
        pid = active_plan(env, "ES L 5000 4998 5010 1 plan", at=at - 30)
        t += [M.trade(at, "BUY", 5000, fill=f"o{i}", order=f"E{i}", plan=pid), M.trade(at + 100, "SELL", 4998, fill=f"c{i}", order=f"X{i}")]
        if i < 3:
            ev.append(M.oevent(at + 100, f"X{i}", "FILLED", "STOP", 4998))
    M.do_import(env, t, ev)
    k = DC.session_kpis(env.con, M.MAN, M.ACCT, DAY)
    assert (k["closes"], k["adherence_decidable"], k["adherence_unknown"], k["adherence_violations"]) == (5, 3, 2, 0)
    assert k["discipline_events"] == {"MISSING_EVIDENCE": 2} and k["plan_coverage"] == 1.0
    assert k["sample_insufficient"] is False
