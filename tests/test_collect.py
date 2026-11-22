import io
import logging
import re
from contextlib import redirect_stdout
from datetime import date

import pytest

import helpers as H
from trading_agent import sessions
from trading_agent.collect import csv_path, run_collect
from trading_agent.sources import webull
from trading_agent.sources.webull import FetchError

D1, D2 = date(2025, 11, 25), date(2025, 11, 26)
CID = "ESZ2025"


def slots(day, n, start=(9, 30)):
    s = H.et(day, *start)
    return [H.wb_row(s + 300 * i, 5000 + i * 0.25) for i in range(n)]


def collect(env, adapter, now):
    return run_collect(env.settings, env.con, env.scope, adapter, now=now)


def lines(env, day=D1):
    return csv_path(env.settings, "ES", CID, day.isoformat()).read_bytes().splitlines()


# ---------------------------------------------------------------- 7.2 适配器解析（形状为假定，待用真实响应核实）
def test_parse_assumed_shapes():
    rows = [{"time": "2025-11-25T14:30:00Z", "open": "5000", "high": "5001", "low": "4999", "close": "5000.5", "volume": "7"},
            {"time": 1764081300000, "open": 5001, "high": 5002, "low": 5000, "close": 5001.5, "volume": 3}]
    for payload in (rows, {"result": rows, "symbol": "ESZ5"}, [{"symbol": "ESZ5", "result": rows}], {"data": rows}):
        sym, out = webull.parse(payload, "5m")
        assert [r["start"] for r in out] == [H.et(D1, 9, 30), H.et(D1, 9, 35)]
        assert out[0]["close"] == "5000.5" and out[1]["volume"] == "3"
    assert webull.parse([], "5m") == (None, [])
    assert webull.parse({"result": []}, "5m")[1] == []
    with pytest.raises(FetchError, match="BAD_RESPONSE"):
        webull.parse([{"time": "x"}], "5m")


def test_credentials_missing_and_env_file(tmp_path):
    with pytest.raises(FetchError) as e:
        webull.credentials({}, tmp_path)
    assert e.value.code == "CREDENTIALS_MISSING"
    (tmp_path / ".env").write_text("# c\nWEBULL_APP_KEY='k1'\nexport WEBULL_APP_SECRET=\"s1\"\n")
    assert webull.credentials({}, tmp_path) == ("k1", "s1", "us")
    assert webull.credentials({"WEBULL_APP_KEY": "k2", "WEBULL_APP_SECRET": "s2", "WEBULL_REGION": "hk"}, tmp_path) == (
        "k2", "s2", "hk")


def test_missing_credentials_makes_no_request(env):
    ad = webull.WebullBars(env={}, project_root=env.tmp)
    r = collect(env, ad, H.et(D1, 10, 0, 10))
    assert r["ok"] is False and r["error"] == "CREDENTIALS_MISSING" and ad._dc is None
    assert not (env.root / "webull").exists()


# ---------------------------------------------------------------- 7.3 采集逻辑
def test_only_closed_bars_saved(env):
    ad = H.FakeAdapter(slots(D1, 2))                    # 09:30、09:35
    r = collect(env, ad, H.et(D1, 9, 35, 10))
    assert r["ok"] and r["rows_written"] == 1 and r["skipped_unclosed"] == 1 and r["contract_id"] == CID
    body = lines(env)
    assert len(body) == 2 and body[1].split(b",")[3] == b"2025-11-25T14:30:00Z"
    assert ad.calls[0][0] == CID and ad.calls[0][1] == "5m"


def test_uses_active_contract_after_roll(env):
    roll = date(2025, 12, 11)
    ad = H.FakeAdapter(slots(roll, 2))
    r = collect(env, ad, H.et(roll, 9, 45))
    assert ad.calls[0][0] == "ESH2026" and r["contract_id"] == "ESH2026"
    assert csv_path(env.settings, "ES", "ESH2026", "2025-12-11").is_file()


def test_unexpected_contract_writes_nothing(env):
    ad = H.FakeAdapter(slots(D1, 3), returned="ESH6")
    r = collect(env, ad, H.et(D1, 10, 0))
    assert r["ok"] is False and r["error"] == "UNEXPECTED_CONTRACT" and r["rows_written"] == 0
    assert not (env.root / "webull").exists() and env.count() == 0
    a = env.con.execute("SELECT level, kind FROM alerts").fetchall()
    assert [tuple(x) for x in a] == [("WARN", "COLLECT_FAILED")]


@pytest.mark.parametrize("adapter,code", [
    (H.FakeAdapter(error=FetchError("MARKET_DATA_NOT_SUBSCRIBED", "no permission")), "MARKET_DATA_NOT_SUBSCRIBED"),
    (H.FakeAdapter(rows=[]), "EMPTY_RESPONSE")])
def test_failures_write_nothing(env, adapter, code):
    r = collect(env, adapter, H.et(D1, 10, 0))
    assert r["ok"] is False and r["error"] == code
    assert not (env.root / "webull").exists() and env.count() == 0
    assert env.con.execute("SELECT COUNT(*) FROM alerts WHERE kind='COLLECT_FAILED'").fetchone()[0] == 1


# ---------------------------------------------------------------- 7.4 CSV 落盘
def test_append_only_and_no_duplicates(env):
    ad = H.FakeAdapter(slots(D1, 4))
    collect(env, ad, H.et(D1, 9, 40, 10))                # 09:30、09:35 已收线
    first = lines(env)
    assert len(first) == 3
    r = collect(env, ad, H.et(D1, 9, 41, 0))             # 没有新线收线
    assert r["rows_written"] == 0 and r["skipped_existing"] == 2 and lines(env) == first
    r = collect(env, ad, H.et(D1, 9, 50, 10))
    assert r["rows_written"] == 2 and r["new_bars"] == 2
    after = lines(env)
    assert after[:3] == first and len(after) == 5         # 已有行逐字节不变
    header = after[0].decode().split(",")
    assert header[:10] == ["instrument_root", "contract_id", "timeframe", "bar_start_utc", "open", "high", "low",
                           "close", "volume", "source"] and "fetched_at_utc" in header


def test_revision_appended_as_new_row(env):
    rows = slots(D1, 2)
    collect(env, H.FakeAdapter(rows), H.et(D1, 9, 40, 10))
    before = lines(env)
    changed = [dict(rows[0], volume="99"), rows[1]]
    r = collect(env, H.FakeAdapter(changed), H.et(D1, 9, 45, 10))
    assert r["revisions"] == 1 and r["rows_written"] == 1
    after = lines(env)
    assert after[:3] == before and len(after) == 4 and b",99," in after[3]
    revs = env.con.execute("SELECT revision, volume, available_at_utc FROM bars WHERE timeframe='5m' AND derived=0"
                           " AND bar_start_utc=? ORDER BY revision", (H.et(D1, 9, 30),)).fetchall()
    assert [tuple(x) for x in revs] == [(1, 10, H.et(D1, 9, 40, 10)), (2, 99, H.et(D1, 9, 45, 10))]


def test_overnight_bars_go_to_right_trading_day_file(env):
    night = [H.wb_row(H.et(D1, 18, 0) + 300 * i, 5000) for i in range(3)]       # D1 18:00 之后属于 D2
    r = collect(env, H.FakeAdapter(night), H.et(D1, 19, 0))
    assert r["files"] == [f"webull/ES/{CID}/2025-11-26.csv"]
    got = env.con.execute("SELECT DISTINCT trading_day, session_type FROM bars WHERE derived=0").fetchall()
    assert [tuple(x) for x in got] == [("2025-11-26", "ETH")]


def test_maintenance_window_rows_skipped(env):
    rows = [H.wb_row(H.et(D1, 17, 30), 5000), H.wb_row(H.et(D1, 15, 55), 5000)]
    r = collect(env, H.FakeAdapter(rows), H.et(D1, 18, 30))
    assert r["skipped_outside"] == 1 and r["rows_written"] == 1


# ---------------------------------------------------------------- 7.5 补缺
def test_backfill_after_missed_runs(env):
    ad = H.FakeAdapter(slots(D1, 12))
    collect(env, ad, H.et(D1, 9, 40, 10))                 # 取到 09:30、09:35
    r = collect(env, ad, H.et(D1, 10, 0, 10))             # 漏跑 09:45、09:50、09:55 三次后恢复
    assert r["new_bars"] == 4 and r["skipped_existing"] == 2
    av = dict(env.con.execute("SELECT bar_start_utc, available_at_utc FROM bars WHERE timeframe='5m' AND derived=0").fetchall())
    assert len(av) == 6
    assert av[H.et(D1, 9, 30)] == H.et(D1, 9, 40, 10)
    for m in (40, 45, 50, 55):                            # 补回的线：可用时点是补回时刻，不倒填
        assert av[H.et(D1, 9, m)] == H.et(D1, 10, 0, 10)
    assert not env.con.execute("SELECT COUNT(*) FROM bars WHERE available_at_estimated=1 AND derived=0").fetchone()[0]


def test_lookback_window_spans_two_trading_days(env):
    ad = H.FakeAdapter(slots(D1, 78) + slots(D2, 3))
    collect(env, ad, H.et(D2, 9, 45, 10))
    start = ad.calls[0][3]
    assert start == H.et(date(2025, 11, 24), 18, 0)       # 回看 2 个交易日：从前一交易日的 Globex 开盘起
    assert csv_path(env.settings, "ES", CID, "2025-11-25").is_file() and csv_path(env.settings, "ES", CID, "2025-11-26").is_file()


def test_gap_beyond_lookback_stays_a_gap(env):
    ad = H.FakeAdapter(slots(D2, 6))                       # 来源给不出更早的 K 线
    collect(env, ad, H.et(D2, 10, 0, 10))
    assert env.count("trading_day='2025-11-25'") == 0


# ---------------------------------------------------------------- 7.6 collect 子命令
def test_collect_then_scan_sees_bars(env):
    ad = H.FakeAdapter(slots(D1, 6))
    r = collect(env, ad, H.et(D1, 10, 0, 10))
    assert r["ok"] and r["ingest"]["bars_inserted"] == 6
    s = env.scan(H.et(D1, 10, 0, 20))
    assert s["signal_status"] == "WAIT" and s["source"] == "webull" and s["bar_count"] == 6
    assert s["latest_bar"]["available_at_estimated"] is False


def test_collect_cli_exit_codes(env, capsys):
    from trading_agent.cli import main
    assert main(["collect", "--json"], settings=env.settings, adapter=H.FakeAdapter(error=FetchError("HTTP_500"))) == 1
    out = capsys.readouterr().out
    assert '"ok": false' in out and "HTTP_500" in out


# ---------------------------------------------------------------- 7.7 只读与凭证
def test_no_trading_api_in_source():
    src = H.PROJECT / "src" / "trading_agent"
    banned = re.compile(
        r"webull\.trade|trade_client|TradeClient|order_operation|place_order|replace_order|cancel_order|"
        r"modify_order|submit_order|preview_order|place_option|batch_place", re.I)
    files = list(src.rglob("*.py"))
    assert len(files) > 10
    for f in files:
        assert not banned.search(f.read_text(encoding="utf-8")), f
    text = (src / "sources" / "webull.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*from (webull[\w.]*) import", text, re.M)
    assert sorted(set(imports)) == ["webull.core.client", "webull.data.data_client"]
    calls = re.findall(r"dc\.(\w+)\.(\w+)\(", text)
    assert calls == [("futures_market_data", "get_futures_history_bars")]


def test_credentials_never_leak(env, monkeypatch, caplog):
    """用假的 SDK 客户端跑完整的 fetch：stdout、日志、CSV、数据库里都不出现凭证。"""
    import sys
    import types

    key, secret = "KEY-abc123-SENSITIVE", "SECRET-xyz789-SENSITIVE"
    seen = {}

    class ApiClient:
        def __init__(self, k, s, region):
            seen["init"] = (k, s, region)
            self._stream_logger_set = None
            self._file_logger_set = None

    class Resp:
        def json(self):
            s = H.et(D1, 9, 30)
            return [{"symbol": "ESZ5", "result": [
                {"time": sessions.iso(s + 300 * i), "open": "5000", "high": "5001", "low": "4999", "close": "5000",
                 "volume": "5"} for i in range(3)]}]

    class Futures:
        def get_futures_history_bars(self, symbols, category, timespan, **kw):
            seen["call"] = (symbols, category, timespan, kw)
            return Resp()

    class DataClient:
        def __init__(self, api):
            # 真 SDK 在这里检查标志；两个都已置位时不会装默认的 stdout 和文件日志
            seen["flags"] = (api._stream_logger_set, api._file_logger_set)
            self.futures_market_data = Futures()

    for name, attrs in (("webull", {}), ("webull.core", {}), ("webull.core.client", {"ApiClient": ApiClient}),
                        ("webull.data", {}), ("webull.data.data_client", {"DataClient": DataClient})):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)

    ad = webull.WebullBars(env={"WEBULL_APP_KEY": key, "WEBULL_APP_SECRET": secret}, project_root=env.tmp)
    buf = io.StringIO()
    with redirect_stdout(buf), caplog.at_level(logging.DEBUG):
        r = collect(env, ad, H.et(D1, 9, 50, 10))
    assert r["ok"] and r["rows_written"] == 3
    assert seen["init"] == (key, secret, "us") and seen["flags"] == (True, True)
    assert seen["call"][0] == ["ESZ5"] and seen["call"][1] == "US_FUTURES" and seen["call"][2] == "M5"
    blob = buf.getvalue() + caplog.text + str(r)
    for p in env.root.rglob("*.csv"):
        blob += p.read_text(encoding="utf-8")
    for tbl in ("alerts", "ingest_runs", "ingest_files"):
        blob += str([tuple(x) for x in env.con.execute(f"SELECT * FROM {tbl}")])
    assert key not in blob and secret not in blob
    assert not list(env.tmp.rglob("webull_*.log")) and not list(H.PROJECT.glob("webull_*.log"))


def test_fetch_error_message_has_no_credentials():
    e = FetchError("INVALID_SYMBOL", "bad symbol")
    assert str(e) == "INVALID_SYMBOL：bad symbol"
