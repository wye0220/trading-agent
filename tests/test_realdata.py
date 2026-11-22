"""需要数据根目录里的真实 Databento 文件。默认不运行：uv run pytest -m realdata"""
import hashlib
import json
from datetime import date

import pytest

import helpers as H
from trading_agent import db, sessions
from trading_agent.config import load_settings, parse_scope
from trading_agent.pipeline import run_ingest
from trading_agent.scan import run_scan

pytestmark = pytest.mark.realdata
JOB = "databento/GLBX-20260905-AKYFXCHGY4"
DBN = "glbx-mdp3-20190905-20260904.ohlcv-1m.dbn"


@pytest.fixture(scope="module")
def real():
    st = load_settings()
    if not (st.data_root / JOB / DBN).is_file():
        pytest.fail(f"找不到真实数据：{st.data_root / JOB / DBN}")
    con = db.open_runtime(st)
    yield st, con
    con.close()


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def test_source_file_matches_manifest_and_is_untouched(real, tmp_path):
    st, con = real
    manifest = json.loads((st.data_root / JOB / "manifest.json").read_text())
    want = {f["filename"]: f["hash"].split(":")[1] for f in manifest["files"]}
    before = sha(st.data_root / JOB / DBN)
    assert before == want[DBN]
    n = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    s = run_ingest(st, con, report_path=tmp_path / "r.md")
    assert f"{JOB}/{DBN}" in s["files"]["skipped"] and s["bars_inserted"] == 0
    assert con.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == n > 3_000_000
    assert sha(st.data_root / JOB / DBN) == before


def test_first_import_summary(real):
    st, con = real
    row = con.execute("SELECT summary_json FROM ingest_runs WHERE summary_json LIKE '%\"spread_symbols_dropped\": 132%'"
                      " ORDER BY started_at_utc LIMIT 1").fetchone()
    assert row, "没有找到首次导入的记录"
    s = json.loads(row[0])
    assert s["spread_symbols_dropped"] == 132 and s["spread_records_dropped"] == 520581
    assert s["bars_inserted"] + s["spread_records_dropped"] + sum(s["anomalies"].values()) == 3_945_840
    assert not con.execute("SELECT COUNT(*) FROM bars WHERE contract_id LIKE '%-%'").fetchone()[0]
    ids = {r[0] for r in con.execute("SELECT DISTINCT contract_id FROM bars WHERE source='databento'")}
    assert {"ESZ2019", "ESH2020", "ESU2026"} <= ids and all(len(c) == 7 for c in ids)


def test_scan_historical_day(real):
    st, con = real
    scope = parse_scope("pa_brooks:ES:RTH:5m", st)
    as_of = H.et(date(2026, 9, 3), 11, 0)
    a, b = run_scan(st, con, scope, as_of), run_scan(st, con, scope, as_of)
    assert a["signal_status"] == "WAIT" and a["contract_id"] == "ESU2026" and a["source"] == "databento"
    assert a["bar_count"] == 17 and a["fixture"] is False and a["run_mode"] == "historical_replay"
    assert a["snapshot_sha256"] == b["snapshot_sha256"]
    assert a["reference_levels"]["prev_day_status"] == "OK" and a["reference_levels"]["overnight_status"] == "OK"
    assert a["adr_valid_days"] == 10 and a["hourly_bars_available"] == 10
    assert a["latest_bar"]["bar_end_utc"] == sessions.iso(H.et(date(2026, 9, 3), 10, 55))


def test_day_level_facts(real):
    st, con = real
    n, complete, degraded = con.execute(
        "SELECT COUNT(*), SUM(complete), SUM(degraded) FROM trading_days WHERE instrument_root='ES'").fetchone()
    assert n == 1760 and complete >= 1750 and degraded == 11
    gaps = [r[0] for r in con.execute("SELECT trading_day FROM trading_days WHERE gap_count>0 ORDER BY 1")]
    assert gaps == ["2020-03-09", "2020-03-12", "2020-03-16", "2020-03-18"]       # 2020 年 3 月的熔断停牌
