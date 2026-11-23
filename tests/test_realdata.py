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
    assert {"ESZ2019", "ESH2020", "ESH2025"} <= ids and "ESU2026" not in ids    # 截止日之后的合约不在库里 and all(len(c) == 7 for c in ids)


def test_scan_historical_day(real):
    st, con = real
    scope = parse_scope("pa_brooks:ES:RTH:5m", st)
    as_of = H.et(date(2024, 12, 30), 11, 0)          # 开发可见截止日（2024-12-31）之内
    a, b = run_scan(st, con, scope, as_of), run_scan(st, con, scope, as_of)
    assert a["signal_status"] == "WAIT" and a["contract_id"] == "ESH2025" and a["source"] == "databento"
    assert a["bar_count"] == 17 and a["fixture"] is False and a["run_mode"] == "historical_replay"
    assert a["snapshot_sha256"] == b["snapshot_sha256"]
    assert a["reference_levels"]["prev_day_status"] == "OK" and a["reference_levels"]["overnight_status"] == "OK"
    assert a["adr_valid_days"] == 10 and a["hourly_bars_available"] == 10
    assert a["latest_bar"]["bar_end_utc"] == sessions.iso(H.et(date(2024, 12, 30), 10, 55))


def test_day_level_facts(real):
    st, con = real
    n, complete, degraded = con.execute(
        "SELECT COUNT(*), SUM(complete), SUM(degraded) FROM trading_days WHERE instrument_root='ES'").fetchone()
    assert n == 1340 and complete == 1336 and degraded == 5          # 截止日之内的交易日
    gaps = [r[0] for r in con.execute("SELECT trading_day FROM trading_days WHERE gap_count>0 ORDER BY 1")]
    assert gaps == ["2020-03-09", "2020-03-12", "2020-03-16", "2020-03-18"]       # 2020 年 3 月的熔断停牌


def test_nothing_after_dev_visible_cutoff(real):
    """开发可见截止日之后的数据只留覆盖和内容摘要，价格不在运行库里。"""
    st, con = real
    cut = st.dev_visible_through
    assert cut == date(2024, 12, 31)
    for table in ("bars", "trading_days", "rth_days", "reference_levels"):
        n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE trading_day > ?", (cut.isoformat(),)).fetchone()[0]
        assert n == 0, table
    # 截止日之后的扫描只会留下不含任何 K 线的空快照
    assert con.execute("SELECT COUNT(*) FROM market_snapshots WHERE trading_day > ? AND bar_count > 0",
                       (cut.isoformat(),)).fetchone()[0] == 0
    days, bars, missing = con.execute(
        "SELECT COUNT(DISTINCT trading_day), SUM(bar_count), SUM(content_sha256 IS NULL) FROM withheld_coverage"
        " WHERE instrument_root='ES'").fetchone()
    assert (days, bars, missing) == (420, 802180, 0)
    scope = parse_scope("pa_brooks:ES:RTH:5m", st)
    r = run_scan(st, con, scope, H.et(date(2026, 9, 3), 11, 0), record_event=False)
    assert r["signal_status"] == "DATA_ERROR" and r["bar_count"] == 0


def test_registered_splits(real):
    from trading_agent.research import config as RC, splits as SP
    st, con = real
    reg = SP.build(st, con, "ES", RC.load_research(st.project_root))
    assert reg["summary"] == {"development_days": 1080, "forward_validation_days": 251, "capsules": 20,
                              "capsule_valid_days": 400, "remainder_valid_days": 14, "candidates_supported": 6}
    caps = [s for s in reg["segments"] if s["kind"] == "CAPSULE"]
    assert caps[0]["start_day"] == "2025-01-02" and all(s["status"] == "SEALED" and s["valid_days"] == 20 for s in caps)
