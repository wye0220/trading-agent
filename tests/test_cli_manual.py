import json

import helpers as H
import manual_helpers as M
import test_engine_runner as T
from trading_agent import sessions
from trading_agent.cli import COMMANDS, IMPLEMENTED, main


def run(env, capsys, *argv, **kw):
    code = main(list(argv), settings=env.settings, **kw)
    return code, capsys.readouterr().out


def test_implemented_commands():
    assert IMPLEMENTED == {"ingest", "collect", "scan", "plan", "watch", "replay", "baseline", "review", "evolve", "memory", "evaluate"} and len(COMMANDS) == 16


def test_plan_quick_json(env, capsys):
    code, out = run(env, capsys, "plan", "ES", "L", "5000", "4995", "5010", "1", "PA", "Spike", "H1", "--account", "U1", "--json")
    r = json.loads(out)
    assert code == 0 and r["plan_id"].startswith("plan_") and r["status"] == "DRAFT" and r["reserved"] is False
    assert r["instrument_root"] == "ES" and r["contract_id"].startswith("ES") and len(r["contract_id"]) == 7
    assert r["check"]["conclusion"] == "BLOCKED_BY_RISK" and r["check"]["risk_reason"] == "BUDGET_NOT_APPROVED"
    p = env.con.execute("SELECT * FROM trader_plans").fetchone()
    assert (p["account_id"], p["reason"], p["side"], p["entry_ticks"]) == ("U1", "PA Spike H1", "LONG", 20000)


def test_plan_text_output_echoes_contract(env, capsys):
    code, out = run(env, capsys, "plan", "ES", "S", "5000", "5004", "4992", "1", "--account", "U1")
    assert code == 0 and "ES 解析为实际合约 ES" in out and "预检只给结论，不下单" in out and "plan --activate plan_" in out


def test_plan_errors(env, capsys):
    code, out = run(env, capsys, "plan", "ES", "L", "5000", "--account", "U1")
    assert code != 0 and "计划缺少字段" in out
    code, out = run(env, capsys, "plan", "ES", "L", "5000", "4995", "5010", "1")
    assert code != 0 and "没有账户标识" in out
    code, out = run(env, capsys, "plan", "ES", "L", "5000.1", "4995", "5010", "1", "--account", "U1", "--json")
    assert code != 0 and json.loads(out)["error"] == "PLAN"
    assert env.con.execute("SELECT COUNT(*) FROM trader_plans").fetchone()[0] == 0


def test_plan_list_activate_cancel(env, capsys):
    _, out = run(env, capsys, "plan", "ES", "L", "5000", "4995", "5010", "1", "x", "--account", "U1", "--json")
    pid = json.loads(out)["plan_id"]
    code, out = run(env, capsys, "plan", "--activate", pid)
    assert code == 1 and "没有激活，保持 DRAFT：BUDGET_NOT_APPROVED" in out            # 默认限额未批准
    code, out = run(env, capsys, "plan", "--list", "--json")
    assert code == 0 and [(p["plan_id"], p["status"]) for p in json.loads(out)["plans"]] == [(pid, "DRAFT")]
    code, out = run(env, capsys, "plan", "--list")
    assert pid in out and "DRAFT" in out
    code, out = run(env, capsys, "plan", "--cancel", pid)
    assert code == 0 and "已取消" in out
    code, out = run(env, capsys, "plan", "--cancel", pid)
    assert code != 0 and "不能取消" in out


def test_watch_once(env, capsys):
    import time
    now = int(time.time())
    M.snapshot(env, now - 3, [M.pos(1)], account="U1")
    code, out = run(env, capsys, "watch", "--once", "--account", "U1", "--json")
    r = json.loads(out)
    assert code == 0 and r["status"] == "OK" and r["interval_seconds"] == 60 and r["stale_after_seconds"] == 120
    assert [a["rule"] for a in r["alerts"]] == ["UNPLANNED_ENTRY"]
    code, out = run(env, capsys, "watch", "--once", "--account", "U1", "--interval", "300")
    assert code == 0 and "间隔 300s 状态 OK" in out


def test_watch_errors_and_missing_snapshot(env, capsys):
    code, out = run(env, capsys, "watch", "--once")
    assert code != 0 and "没有账户标识" in out
    code, out = run(env, capsys, "watch", "--once", "--account", "U1", "--interval", "0")
    assert code != 0 and "轮询间隔必须是正整数秒" in out
    code, out = run(env, capsys, "watch", "--once", "--account", "U1")
    assert code == 0 and "状态 READ_FAILED" in out and "没有账户快照文件" in out


def test_ingest_trades_and_order_events(env, capsys):
    t = T.bar_end(5)
    tf = M.write(env, "trades.csv", [M.trade(t - 600, "BUY", 5000, fill="f1", order="E1"),
                                     M.trade(t - 60, "SELL", 4995, fill="f2", order="S1"),
                                     M.trade(t, "BUY", 5000), M.trade(t, "BUY", 5000)], M.TRADE_COLS)
    ef = M.write(env, "events.csv", [M.oevent(t - 60, "S1", "FILLED", "STOP", 4995)], M.EVENT_COLS)
    bars_before = env.count()
    code, out = run(env, capsys, "ingest", "--trades", str(tf), "--order-events", str(ef), "--json")
    r = json.loads(out)
    assert code == 0 and [x["kind"] for x in r["results"]] == ["order_events", "trades"]
    assert (r["results"][1]["imported"], r["results"][1]["quarantined"], r["results"][0]["imported"]) == (2, 2, 1)
    assert env.count() == bars_before and env.con.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 0
    code, out = run(env, capsys, "ingest", "--trades", str(tf))
    assert code == 0 and "成交：导入 0，跳过 4，隔离 0" in out          # 已隔离过的重复行不再重复隔离
    assert env.con.execute("SELECT COUNT(*) FROM manual_trades WHERE quarantined=1").fetchone()[0] == 2
