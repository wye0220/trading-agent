"""collect --from-mcp：读 Webull MCP 连接器返回的 JSON 文件。"""
import csv
import json
from datetime import date

import pytest

import helpers as H
from trading_agent import sessions
from trading_agent.cli import main
from trading_agent.sources.webull import FetchError
from trading_agent.sources.webull_mcp import McpDump

D = date(2025, 11, 25)                                # ESZ2025 / NQZ2025 是当日实际合约


def t(hh, mm):
    return H.et(D, hh, mm)


def entry(symbol, starts, base=7000.0):
    return {"symbol": symbol, "instrument_id": "1", "delay_minutes": 0, "result": [
        {"time": sessions.iso(s).replace("Z", ".000+0000"), "open": f"{base + i:.2f}", "high": f"{base + i + 1:.2f}",
         "low": f"{base + i - 1:.2f}", "close": f"{base + i + 0.5:.2f}", "volume": str(100 + i)}
        for i, s in reversed(list(enumerate(starts)))]}      # 连接器返回的是新的在前


def dump(path, entries):
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def run(env, capsys, *argv):
    code = main(["collect", *argv, "--json"], settings=env.settings)
    return code, json.loads(capsys.readouterr().out)


def saved(env, root, cid):
    with open(env.root / "webull" / root / cid / f"{D}.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_saves_both_roots_and_skips_unclosed_bar(env, capsys):
    starts = [t(9, 30), t(9, 35), t(9, 40)]
    f = dump(env.tmp / "bars.json", [entry("NQZ5", starts, 25000), entry("ESZ5", starts)])
    code, r = run(env, capsys, "--from-mcp", f, "--fetched-at", sessions.iso(t(9, 44)))
    assert code == 0 and r["ok"] and [x["instrument_root"] for x in r["results"]] == ["ES", "NQ"]
    for x in r["results"]:
        assert (x["rows_fetched"], x["new_bars"], x["skipped_unclosed"]) == (3, 2, 1)     # 09:40 那根还没收线
    es, nq = saved(env, "ES", "ESZ2025"), saved(env, "NQ", "NQZ2025")
    assert [x["bar_start_utc"] for x in es] == [sessions.iso(t(9, 30)), sessions.iso(t(9, 35))]     # 按时间升序
    assert es[0]["open"] == "7000" and es[0]["source"] == "webull" and es[0]["fetched_at_utc"] == sessions.iso(t(9, 44))
    assert nq[1]["close"] == "25001.5" and nq[0]["contract_id"] == "NQZ2025"
    got = env.con.execute("SELECT instrument_root, COUNT(*), MIN(available_at_estimated), MIN(available_at_utc)"
                          " FROM bars WHERE derived=0 GROUP BY 1 ORDER BY 1").fetchall()
    assert [tuple(x) for x in got] == [("ES", 2, 0, t(9, 44)), ("NQ", 2, 0, t(9, 44))]     # 真实取数时刻，不是估计


def test_repeat_appends_only_new_and_revised(env, capsys):
    starts = [t(9, 30), t(9, 35), t(9, 40)]
    f = dump(env.tmp / "a.json", [entry("ESZ5", starts)])
    run(env, capsys, "--from-mcp", f, "--fetched-at", sessions.iso(t(9, 44)))
    e2 = entry("ESZ5", starts + [t(9, 45)])
    e2["result"][-1]["volume"] = "999"                # 09:30 那根被修订
    f2 = dump(env.tmp / "b.json", [e2])
    code, r = run(env, capsys, "--from-mcp", f2, "--fetched-at", sessions.iso(t(9, 51)))
    x = r["results"][0]
    assert code == 0 and (x["new_bars"], x["revisions"], x["skipped_existing"], x["skipped_unclosed"]) == (2, 1, 1, 0)
    rows = saved(env, "ES", "ESZ2025")
    assert len(rows) == 5 and [x["volume"] for x in rows if x["bar_start_utc"] == sessions.iso(t(9, 30))] == ["100", "999"]
    assert env.con.execute("SELECT MAX(revision) FROM bars WHERE bar_start_utc=?", (t(9, 30),)).fetchone()[0] == 2
    code, r = run(env, capsys, "--from-mcp", f2, "--fetched-at", sessions.iso(t(9, 56)))
    assert r["results"][0]["rows_written"] == 0 and len(saved(env, "ES", "ESZ2025")) == 5     # 重复运行不再追加


def test_rejects_wrong_contract_and_bad_files(env, capsys):
    f = dump(env.tmp / "old.json", [entry("ESH6", [t(9, 30)])])           # 不是当日实际合约
    code, r = run(env, capsys, "--from-mcp", f, "--fetched-at", sessions.iso(t(9, 44)))
    assert code == 1 and r["results"][0]["error"] == "CONTRACT_NOT_IN_DUMP" and r["results"][0]["rows_written"] == 0
    assert not (env.root / "webull").exists()
    f = dump(env.tmp / "unk.json", [entry("ZZZ5", [t(9, 30)]), entry("ESZ5", [t(9, 30)])])
    code, r = run(env, capsys, "--from-mcp", f, "--fetched-at", sessions.iso(t(9, 44)))
    assert code == 1 and {x["instrument_root"]: x["ok"] for x in r["results"]} == {"ES": True, "ZZ": False}
    for i, bad in enumerate(("not json", json.dumps({"a": 1}), json.dumps([{"symbol": "ESZ5"}]),
                             json.dumps([entry("ESZ5", [t(9, 30)]), entry("ESZ5", [t(9, 30)])]))):
        p = env.tmp / f"bad{i}.json"
        p.write_text(bad, encoding="utf-8")
        code, r = run(env, capsys, "--from-mcp", str(p))
        assert code == 1 and r["error"] == "BAD_DUMP"
    code, r = run(env, capsys, "--from-mcp", str(env.tmp / "missing.json"))
    assert code == 1 and r["error"] == "BAD_DUMP"
    e = entry("ESZ5", [t(9, 30)])
    del e["result"][0]["close"]
    with pytest.raises(FetchError, match="缺字段"):
        McpDump(dump(env.tmp / "nf.json", [e]), D).fetch("ESZ2025", "5m", 1, 0, 0)


def test_after_cutoff_bars_are_saved_but_not_loaded(tmp_path, capsys):
    """开发可见截止日之后：文件照存（留给以后的评测），价格不进运行库，只记覆盖和摘要。"""
    from conftest import Env
    e = Env(tmp_path, dev_visible_through=date(2025, 11, 24))
    f = dump(e.tmp / "x.json", [entry("ESZ5", [t(9, 30), t(9, 35)])])
    code, r = run(e, capsys, "--from-mcp", f, "--fetched-at", sessions.iso(t(9, 44)))
    assert code == 0 and r["results"][0]["rows_written"] == 2 and len(saved(e, "ES", "ESZ2025")) == 2
    assert e.count() == 0
    w = e.con.execute("SELECT bar_count, content_sha256 FROM withheld_coverage").fetchone()
    assert w["bar_count"] == 2 and len(w["content_sha256"]) == 64
    e.con.close()


def test_adapter_module_has_no_network_or_trading_calls():
    text = (H.PROJECT / "src" / "trading_agent" / "sources" / "webull_mcp.py").read_text(encoding="utf-8")
    for banned in ("import requests", "urllib", "socket", "place_", "cancel_", "replace_order", "import webull"):
        assert banned not in text, banned


def test_mcp_request_prints_active_contracts_without_writing(env, capsys):
    code = main(["collect", "--mcp-request", "--json"], settings=env.settings)
    r = json.loads(capsys.readouterr().out)
    assert code == 0 and r["category"] == "US_FUTURES" and r["timespan"] == "M5" and 2 <= r["count"] <= 1200
    es, nq = r["symbols"].split(",")
    assert es.startswith("ES") and nq.startswith("NQ") and len(es) == 4 and es[2:] == nq[2:]
    assert r["start_time"].endswith(".000Z") and sessions.parse_utc(r["start_time"]) % 300 == 0
    assert not (env.root / "webull").exists() and env.count() == 0


def test_check_day_reports_completeness(env, capsys):
    full = sessions.expected_rth_slots(D)
    f = dump(env.tmp / "full.json", [entry("ESZ5", full), entry("NQZ5", full[:-1], 25000)])
    run(env, capsys, "--from-mcp", f, "--fetched-at", sessions.iso(H.et(D, 17, 5)))
    code, r = run(env, capsys, "--check-day", D.isoformat())
    by = {x["instrument_root"]: x for x in r["results"]}
    assert code == 1 and not r["ok"] and by["ES"]["ok"] and by["ES"]["rth_missing"] == 0
    assert by["NQ"]["rth_missing"] == 1 and not by["NQ"]["ok"] and by["NQ"]["contract_id"] == "NQZ2025"
    code, r = run(env, capsys, "--check-day", "2025-11-27")           # 感恩节：不是交易日，不需要数据
    assert code == 0 and r["is_trading_day"] is False
    code, r = run(env, capsys, "--check-day", "nope")
    assert code == 2


def test_ops_scripts_are_read_only_and_keep_secrets_out():
    ops = H.PROJECT / "ops"
    run_sh = (ops / "launchd" / "run-collect-mcp.sh").read_text(encoding="utf-8")
    notify = (ops / "notify-telegram.sh").read_text(encoding="utf-8")
    allowed = run_sh.split("--allowedTools", 1)[1].split("--model", 1)[0]
    assert "get_futures_bars" in allowed and "collect --from-mcp" in allowed
    for banned in ("place_", "revoke_", "cancel", "order", "Write", "Edit", "Bash(*)", "dangerously"):
        assert banned not in allowed, banned                     # 无人值守时只允许取 K 线和落盘
    assert "--check-day" in run_sh and "notify" in run_sh        # 结果独立核对，失败时通知
    assert "source " not in notify and "echo \"$token" not in notify and "--form-string" in notify
    assert not __import__("re").search(r"\d{6,}:[A-Za-z0-9_-]{20,}", run_sh + notify)      # 没有写死的 token
