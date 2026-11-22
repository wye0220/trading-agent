import hashlib
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import helpers as H
from trading_agent import sessions
from trading_agent.bars import OffTickPrice, decimal_to_ticks, fixed_to_ticks
from trading_agent.ingest import HOLDOUT_NOT_SET, discover

D1, D2 = date(2025, 11, 25), date(2025, 11, 26)
TICK = Decimal("0.25")
MAP = {"ESZ5": [(date(2025, 1, 1), date(2026, 1, 1), 111)]}


def minute_recs(day, iid=111, n=390, base=5000.0):
    s = sessions.rth_bounds(day)[0]
    return [(iid, s + 60 * i, base, base + 1, base - 1, base + 0.5, 5) for i in range(n)]


def sha_all(root: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in discover(root)}


# ---------------------------------------------------------------- 6.1 价格换算
def test_price_conversion():
    assert fixed_to_ticks(2939250000000, TICK) == 11757
    with pytest.raises(OffTickPrice):
        fixed_to_ticks(2939100000000, TICK)
    assert decimal_to_ticks("5000.25", TICK) == 20001 and decimal_to_ticks(5000, TICK) == 20000
    with pytest.raises(OffTickPrice):
        decimal_to_ticks("5000.10", TICK)
    with pytest.raises(OffTickPrice):
        decimal_to_ticks("abc", TICK)


def test_off_tick_rows_not_stored(env):
    rows = [H.bar(H.et(D1, 9, 30), 5000.0), H.bar(H.et(D1, 9, 35), 5000.1)]
    H.write_csv(env.root / "fixture_a.csv", rows)
    s = env.ingest()
    assert s["bars_inserted"] == 1 and s["anomalies"] == {"OFF_TICK_PRICE": 1}


# ---------------------------------------------------------------- 6.3 DBN
def test_dbn_import_contract_prices_and_spreads(env):
    recs = minute_recs(D1, n=10) + [(222, sessions.rth_bounds(D1)[0], 1, 1.25, 0.75, 1, 3)]
    H.write_dbn(env.root / "dbn" / "fixture.ohlcv-1m.dbn",
                {**MAP, "ESZ5-ESH6": [(date(2025, 1, 1), date(2026, 1, 1), 222)]}, recs)
    s = env.ingest()
    assert s["bars_inserted"] == 10 and s["spread_symbols_dropped"] == 1 and s["spread_records_dropped"] == 1
    r = env.con.execute("SELECT * FROM bars WHERE timeframe='1m' ORDER BY bar_start_utc LIMIT 1").fetchone()
    assert (r["source"], r["contract_id"], r["instrument_root"]) == ("databento", "ESZ2025", "ES")
    assert (r["open"], r["high"], r["low"], r["close"], r["volume"]) == (20000, 20004, 19996, 20002, 5)
    assert r["session_type"] == "RTH" and r["trading_day"] == "2025-11-25" and r["fixture"] == 1
    assert not env.count("contract_id LIKE '%-%'")
    f = env.con.execute("SELECT * FROM ingest_files").fetchone()
    assert f["data_schema"] == "GLBX.MDP3/ohlcv-1m" and f["status"] == "IMPORTED" and f["records"] == 11


def test_dbn_same_code_ten_years_apart(env):
    a, b = date(2020, 1, 15), date(2025, 1, 15)
    H.write_dbn(env.root / "fixture_decade.dbn",
                {"ESH0": [(date(2019, 9, 5), date(2020, 4, 5), 10571), (date(2024, 12, 8), date(2026, 9, 5), 42000746)]},
                minute_recs(a, 10571, 3) + minute_recs(b, 42000746, 3))
    env.ingest()
    got = dict(env.con.execute("SELECT contract_id, COUNT(*) FROM bars WHERE timeframe='1m' GROUP BY 1").fetchall())
    assert got == {"ESH2020": 3, "ESH2030": 3}


def test_dbn_unmapped_and_unsupported_schema(env):
    H.write_dbn(env.root / "fixture_unmapped.dbn", {"ESZ5": [(date(2025, 12, 1), date(2026, 1, 1), 111)]},
                minute_recs(D1, n=4))                       # 记录日期在映射区间之外
    H.write_dbn(env.root / "fixture_trades.dbn", MAP, [], schema="trades")
    H.write_dbn(env.root / "fixture_ok.dbn", MAP, minute_recs(D2, n=5))
    s = env.ingest()
    assert s["anomalies"]["UNMAPPED_INSTRUMENT"] == 4
    assert s["files_unsupported"] == 1 and s["files"]["unsupported"][0]["schema"] == "trades"
    assert s["bars_inserted"] == 5                           # 其余文件照常导入
    st = dict(env.con.execute("SELECT path, status FROM ingest_files").fetchall())
    assert st["fixture_trades.dbn"] == "UNSUPPORTED_SCHEMA"


def test_dbn_zst(env):
    import zstandard
    p = H.write_dbn(env.tmp / "plain.dbn", MAP, minute_recs(D1, n=7))
    out = env.root / "fixture_z.ohlcv-1m.dbn.zst"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(zstandard.ZstdCompressor().compress(p.read_bytes()))
    assert env.ingest()["bars_inserted"] == 7


# ---------------------------------------------------------------- 6.4 CSV
def test_csv_ok_and_missing_column(env):
    H.write_csv(env.root / "fixture_ok.csv", H.rth_bars(D1)[:5])
    H.write_csv(env.root / "fixture_bad.csv", H.rth_bars(D2)[:5], columns=[c for c in H.COLUMNS if c != "contract_id"])
    s = env.ingest()
    assert s["bars_inserted"] == 5 and s["files_rejected"] == 1
    assert "contract_id" in s["files"]["rejected"][0]["reason"]
    assert not env.count("trading_day=?", D2.isoformat())
    r = env.con.execute("SELECT * FROM bars LIMIT 1").fetchone()
    assert r["source"] == "fixsrc" and r["available_at_estimated"] == 1
    assert r["available_at_utc"] == r["bar_end_utc"] + env.settings.available_at_delay_seconds


def test_csv_fetched_at_becomes_available_at(env):
    s0 = H.et(D1, 9, 30)
    H.write_csv(env.root / "fixture_f.csv", [
        H.bar(s0, 5000, fetched=s0 + 310, source="webull"),
        H.bar(s0, 5000, fetched=s0 + 610, source="webull"),          # 同内容再次取到：保留最早的
        H.bar(s0, 5001, fetched=s0 + 910, source="webull")])          # 内容变了：新 revision
    s = env.ingest()
    assert (s["bars_inserted"], s["revisions_added"], s["duplicates_skipped"]) == (1, 1, 1)
    rows = env.con.execute("SELECT revision, open, available_at_utc, available_at_estimated FROM bars"
                           " WHERE derived=0 ORDER BY revision").fetchall()
    assert [tuple(r) for r in rows] == [(1, 20000, s0 + 310, 0), (2, 20004, s0 + 910, 0)]


# ---------------------------------------------------------------- 6.5 入库
def test_reimport_is_idempotent(env):
    H.write_csv(env.root / "fixture_a.csv", H.rth_bars(D1))
    env.ingest()
    n = env.count()
    dump = env.con.execute("SELECT * FROM bars ORDER BY 1,2,3,4,5").fetchall()
    s = env.ingest()
    assert s["files_skipped"] == 1 and s["files_imported"] == 0 and s["bars_inserted"] == 0
    assert env.count() == n
    assert [tuple(r) for r in env.con.execute("SELECT * FROM bars ORDER BY 1,2,3,4,5")] == [tuple(r) for r in dump]


def test_overlapping_file_same_content_adds_nothing(env):
    H.write_csv(env.root / "fixture_a.csv", H.rth_bars(D1))
    env.ingest()
    n = env.count("derived=0")
    H.write_csv(env.root / "fixture_b.csv", H.rth_bars(D1))
    s = env.ingest()
    assert s["bars_inserted"] == 0 and s["duplicates_skipped"] == 78 and env.count("derived=0") == n


def test_changed_bar_creates_revision_and_keeps_old(env):
    rows = H.rth_bars(D1)
    H.write_csv(env.root / "fixture_a.csv", rows)
    env.ingest()
    rows2 = [dict(r) for r in rows]
    rows2[3]["close"] = "5000.75"
    H.write_csv(env.root / "fixture_b.csv", rows2)
    s = env.ingest()
    assert s["revisions_added"] == 1
    got = env.con.execute("SELECT revision, close FROM bars WHERE derived=0 AND bar_start_utc=? ORDER BY revision",
                          (sessions.parse_utc(rows[3]["bar_start_utc"]),)).fetchall()
    assert len(got) == 2 and got[0]["close"] == decimal_to_ticks(rows[3]["close"], TICK) and got[1]["close"] == 20003


def test_conflict_in_one_file_stores_neither(env):
    s0 = sessions.rth_bounds(D1)[0]
    recs = minute_recs(D1, n=3) + [(111, s0, 5010, 5011, 5009, 5010, 9)]
    H.write_dbn(env.root / "fixture_conf.dbn", MAP, recs)
    s = env.ingest()
    assert s["anomalies"] == {"CONFLICTING_DUPLICATE": 1} and s["bars_inserted"] == 2
    assert not env.count("timeframe='1m' AND bar_start_utc=?", s0)


def test_ohlc_inconsistent_and_outside_session(env):
    H.write_csv(env.root / "fixture_bad.csv", [
        H.bar(H.et(D1, 9, 30), 5000, high=4999),                       # 最高价低于开盘价
        H.bar(H.et(D1, 17, 30), 5000),                                 # 每日维护时段
        H.bar(H.et(date(2025, 11, 29), 10, 0), 5000),                  # 周六
        H.bar(H.et(D1, 9, 35), 5000)])
    s = env.ingest()
    assert s["bars_inserted"] == 1
    assert s["anomalies"] == {"OHLC_INCONSISTENT": 1, "OUTSIDE_SESSION": 2}


# ---------------------------------------------------------------- 6.6 增量
def test_only_new_file_is_read(env):
    H.write_csv(env.root / "fixture_a.csv", H.rth_bars(D1))
    env.ingest()
    H.write_csv(env.root / "fixture_b.csv", H.rth_bars(D2))
    s = env.ingest()
    assert s["files"]["imported"] == ["fixture_b.csv"] and s["files"]["skipped"] == ["fixture_a.csv"]
    assert s["bars_inserted"] == 78


def test_appended_csv_reads_only_new_rows(env):
    rows = H.rth_bars(D1)
    p = H.write_csv(env.root / "fixture_a.csv", rows[:10])
    env.ingest()
    H.write_csv(p, rows[10:13], mode="a")
    s = env.ingest()
    assert s["bars_inserted"] == 3 and s["duplicates_skipped"] == 0       # 没有重读前 10 行
    assert env.count("derived=0") == 13
    assert env.con.execute("SELECT rows_processed FROM ingest_files").fetchone()[0] == 13


def test_rewritten_prefix_is_fully_reread(env):
    rows = H.rth_bars(D1)
    p = H.write_csv(env.root / "fixture_a.csv", rows[:10])
    env.ingest()
    changed = [dict(r) for r in rows[:12]]
    changed[0]["close"] = "5000.75"
    H.write_csv(p, changed)
    s = env.ingest()
    assert s["bars_inserted"] == 2 and s["revisions_added"] == 1 and s["duplicates_skipped"] == 9
    assert env.count("derived=0") == 13


# ---------------------------------------------------------------- 6.7 每日单一来源
def test_one_source_per_day(env):
    H.write_dbn(env.root / "fixture_db.dbn", MAP, minute_recs(D1))
    H.write_csv(env.root / "fixture_wb.csv",
                H.rth_bars(D1, source="webull", fetched=H.et(D1, 16, 1))
                + H.rth_bars(D2, source="webull", fetched=H.et(D2, 16, 1)))
    env.ingest()
    src = dict(env.con.execute("SELECT trading_day, source FROM trading_days").fetchall())
    assert src == {"2025-11-25": "databento", "2025-11-26": "webull"}       # 重叠日取优先来源；只有一个来源时用它
    r1 = env.scan(H.et(D1, 16, 30))
    assert r1["source"] == "databento" and r1["bar_count"] == 78
    snap = env.con.execute("SELECT source FROM market_snapshots WHERE snapshot_id=?", (r1["snapshot_id"],)).fetchone()
    assert snap[0] == "databento"
    assert env.scan(H.et(D2, 16, 30))["source"] == "webull"


# ---------------------------------------------------------------- 6.8 截止日
def test_cutoff_withholds_later_bars(tmp_path):
    from conftest import Env
    e = Env(tmp_path)
    H.write_csv(e.root / "real.csv", H.rth_bars(D1, fixture=False) + H.rth_bars(D2, fixture=False))
    s = e.ingest(through=D1)
    assert s["dev_visible_through"] == "2025-11-25" and s["flags"] == [] and s["withheld_after_cutoff"] == 78
    assert e.count("trading_day>?", D1.isoformat()) == 0 and e.count("derived=0") == 78
    w = e.con.execute("SELECT * FROM withheld_coverage").fetchone()
    assert (w["trading_day"], w["bar_count"], w["rth_slots_present"]) == ("2025-11-26", 78, 78)
    assert not [c for c in w.keys() if c in ("open", "high", "low", "close")]
    e.con.close()


def test_no_cutoff_imports_all_and_flags(env):
    H.write_csv(env.root / "real.csv", H.rth_bars(D1, fixture=False) + H.rth_bars(D2, fixture=False))
    s = env.ingest()
    assert s["flags"] == [HOLDOUT_NOT_SET] and env.count("derived=0") == 156
    assert HOLDOUT_NOT_SET in env.report.read_text(encoding="utf-8")


def test_fixture_not_subject_to_cutoff(env):
    H.write_csv(env.root / "fixture_a.csv", H.rth_bars(D2))
    assert env.ingest(through=D1)["bars_inserted"] == 78


# ---------------------------------------------------------------- 6.9 合成样本
def test_committed_fixtures_are_marked(env):
    import shutil
    src = H.PROJECT / "tests" / "fixtures"
    files = sorted(src.glob("*.csv")) + sorted(src.glob("*.dbn"))
    assert len(files) >= 7
    for f in files:
        shutil.copy(f, env.root / f.name)
    s = env.ingest()
    assert s["bars_inserted"] > 0
    assert env.count("fixture=0") == 0
    assert env.con.execute("SELECT COUNT(*) FROM trading_days WHERE fixture=0").fetchone()[0] == 0
    assert "NO_REAL_DATA" in env.report.read_text(encoding="utf-8")
    r = env.scan(H.et(date(2025, 11, 25), 12, 0))
    assert r["fixture"] is True and r["run_mode"] == "fixture" and "[FIXTURE]" in r["report"]
    assert env.con.execute("SELECT fixture FROM market_snapshots").fetchone()[0] == 1


# ---------------------------------------------------------------- 6.10 只读
def test_ingest_leaves_sources_untouched(env):
    H.write_dbn(env.root / "a" / "fixture.dbn", MAP, minute_recs(D1, n=20))
    H.write_csv(env.root / "b" / "fixture.csv", H.rth_bars(D2))
    before = sha_all(env.root)
    names = sorted(str(p.relative_to(env.root)) for p in env.root.rglob("*") if p.is_file() and "runtime" not in p.parts)
    env.ingest()
    assert sha_all(env.root) == before
    assert sorted(str(p.relative_to(env.root)) for p in env.root.rglob("*")
                  if p.is_file() and "runtime" not in p.parts) == names


def test_ingest_code_path_has_no_network_imports():
    """ingest 用到的模块不引用任何网络库，也不对来源文件做写、删、移动。"""
    src = H.PROJECT / "src" / "trading_agent"
    mods = ["ingest.py", "pipeline.py", "derive.py", "days.py", "context.py", "report.py", "scan.py", "bars.py",
            "sessions.py", "contracts.py", "db.py", "sources/dbn.py", "sources/csv_source.py", "sources/base.py"]
    net = re.compile(r"^\s*(import|from)\s+(requests|urllib|http|socket|ssl|httpx|aiohttp|websocket|webull|ftplib)\b", re.M)
    destructive = re.compile(r"\.unlink\(|os\.remove|shutil\.(move|rmtree|copy)|\.rename\(|\.rmdir\(")
    for m in mods:
        text = (src / m).read_text(encoding="utf-8")
        assert not net.search(text), m
        assert not destructive.search(text), m
    for m in ("ingest.py", "sources/dbn.py", "sources/csv_source.py"):
        text = (src / m).read_text(encoding="utf-8")
        assert not re.search(r"open\([^)]*[\"'](w|a|wb|ab|r\+)[\"']", text), m
        assert "write_text" not in text and "write_bytes" not in text, m
