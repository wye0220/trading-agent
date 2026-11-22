import json
from datetime import date

import pytest

import helpers as H
from trading_agent import sessions

D1, D2 = date(2025, 11, 25), date(2025, 11, 26)
HALF = date(2025, 11, 28)
MAP = {"ESZ5": [(date(2025, 1, 1), date(2026, 1, 1), 111)]}


def cats(env, category=None):
    q = "SELECT category, trading_day, start_utc, end_utc, detail FROM data_anomalies"
    rows = env.con.execute(q + (" WHERE category=?" if category else ""), (category,) if category else ()).fetchall()
    return [tuple(r) for r in rows]


# ---------------------------------------------------------------- 9.1 逐条校验（另见 test_ingest）
@pytest.mark.parametrize("bad,category", [
    (dict(high=4999), "OHLC_INCONSISTENT"), (dict(low=5001), "OHLC_INCONSISTENT"),
    (dict(close=5002), "OHLC_INCONSISTENT"), (dict(volume=-1), "OHLC_INCONSISTENT")])
def test_bad_rows_flagged(env, bad, category):
    H.write_csv(env.root / "fixture.csv", [H.bar(H.et(D1, 9, 30), 5000, **bad), H.bar(H.et(D1, 9, 35), 5000)])
    s = env.ingest()
    assert s["anomalies"] == {category: 1} and s["bars_inserted"] == 1
    a = cats(env, category)[0]
    assert a[2] == H.et(D1, 9, 30) and a[3] == H.et(D1, 9, 35)


def test_clean_rows_not_flagged(env):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(D1))
    assert env.ingest()["anomalies"] == {} and cats(env) == []


# ---------------------------------------------------------------- 9.2 时间轴与缺口
def test_gap_in_rth(env):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(D1, skip=[(10, 15)]))
    env.ingest()
    assert cats(env, "UNEXPECTED_GAP") == [("UNEXPECTED_GAP", "2025-11-25", H.et(D1, 10, 15), H.et(D1, 10, 20),
                                            "缺 1 根 5 分钟线")]
    d = env.con.execute("SELECT * FROM trading_days").fetchone()
    assert (d["rth_expected"], d["rth_present"], d["gap_count"], d["complete"], d["reason"]) == (
        78, 77, 1, 0, "UNEXPECTED_GAP")
    assert env.count("bar_start_utc=?", H.et(D1, 10, 15)) == 0               # 不填充


def test_half_day_after_close_is_not_a_gap(env):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(HALF))
    env.ingest()
    d = env.con.execute("SELECT * FROM trading_days").fetchone()
    assert (d["rth_expected"], d["rth_present"], d["gap_count"], d["early_close"], d["complete"]) == (42, 42, 0, 1, 1)
    assert cats(env, "UNEXPECTED_GAP") == []
    assert env.scan(H.et(HALF, 15, 0))["signal_status"] == "WAIT"


def test_dst_weeks_no_false_gaps(env):
    days = [date(2025, 3, 7), date(2025, 3, 10), date(2025, 10, 31), date(2025, 11, 3)]
    rows = []
    for d in days:
        c = "ESH2025" if d < date(2025, 3, 17) else "ESZ2025"
        rows += H.rth_bars(d, contract=c)
    H.write_csv(env.root / "fixture.csv", rows)
    env.ingest()
    got = env.con.execute("SELECT trading_day, rth_present, gap_count, complete FROM trading_days ORDER BY 1").fetchall()
    assert [tuple(r) for r in got] == [(d.isoformat(), 78, 0, 1) for d in days]
    assert cats(env) == []


def test_thin_bar_hint(env):
    s = sessions.rth_bounds(D1)[0]
    recs = [(111, s, 5000, 5001, 4999, 5000, 5)] + [(111, s + 300 + 60 * i, 5000, 5001, 4999, 5000, 5) for i in range(5)]
    H.write_dbn(env.root / "fixture.dbn", MAP, recs)
    env.ingest()
    assert [(a[0], a[2]) for a in cats(env, "THIN_BAR")] == [("THIN_BAR", s)]
    assert env.con.execute("SELECT thin_count FROM trading_days").fetchone()[0] == 1


# ---------------------------------------------------------------- 9.3 换月一致性
def test_wrong_contract(env):
    roll = date(2025, 12, 15)                    # roll-v2 指定 ESH2026
    H.write_csv(env.root / "fixture.csv", H.rth_bars(roll, contract="ESZ2025"))
    env.ingest()
    d = env.con.execute("SELECT * FROM trading_days").fetchone()
    assert (d["contract_id"], d["source"], d["wrong_contract"], d["complete"], d["reason"]) == (
        "ESH2026", None, 1, 0, "WRONG_CONTRACT")
    a = cats(env, "WRONG_CONTRACT")
    assert len(a) == 1 and "ESZ2025" in a[0][4] and "ESH2026" in a[0][4]
    r = env.scan(H.et(roll, 12, 0))
    assert r["signal_status"] == "DATA_ERROR" and r["problems"][0]["category"] == "WRONG_CONTRACT"


# ---------------------------------------------------------------- 9.4 降级日
def test_degraded_day_from_condition_json(tmp_path):
    from conftest import Env
    e = Env(tmp_path)
    H.write_dbn(e.root / "job" / "real.ohlcv-1m.dbn", MAP,
                [(111, sessions.rth_bounds(d)[0] + 60 * i, 5000, 5001, 4999, 5000, 5) for d in (D1, D2) for i in range(390)])
    (e.root / "job" / "condition.json").write_text(json.dumps([
        {"date": "2025-11-25", "condition": "degraded", "last_modified_date": "2026-01-01"},
        {"date": "2025-11-26", "condition": "available", "last_modified_date": "2026-01-01"}]))
    e.ingest()
    got = dict(e.con.execute("SELECT trading_day, degraded FROM trading_days").fetchall())
    assert got == {"2025-11-25": 1, "2025-11-26": 0}
    assert e.scan(H.et(D1, 16, 30))["degraded_day"] is True
    text = e.report.read_text(encoding="utf-8")
    assert "### 降级日" in text and "2025-11-25" in text.split("### 降级日")[1].split("###")[0]
    e.con.close()


# ---------------------------------------------------------------- 9.5 新鲜度与阻止状态
def status(env, domain="fixture"):
    r = env.con.execute("SELECT blocked, reason, version FROM scope_data_status WHERE risk_domain=?", (domain,)).fetchone()
    return tuple(r) if r else None


def test_latest_bar_missing_and_stale(env):
    fetched = lambda s: s + 310   # noqa: E731
    rows = [H.bar(s, 5000, source="webull", fetched=fetched(s)) for s in sessions.expected_rth_slots(D1)[:6]]
    H.write_csv(env.root / "fixture.csv", rows)                 # 最后一根是 09:55–10:00
    env.ingest()
    ok = env.scan(H.et(D1, 10, 0, 30))
    assert ok["signal_status"] == "WAIT" and ok["new_entries_blocked"] is False
    one = env.scan(H.et(D1, 10, 5, 30))                         # 10:00 那根应已收线但不存在
    assert one["signal_status"] == "DATA_ERROR" and one["problems"][0]["category"] == "LATEST_BAR_MISSING"
    stale = env.scan(H.et(D1, 10, 16, 0))                       # 落后 3 根
    assert stale["signal_status"] == "DATA_ERROR" and stale["problems"][0]["category"] == "STALE_DATA"
    assert "新鲜度不足" in stale["problems"][0]["detail"] and stale["new_entries_blocked"] is True


def test_block_and_recover(env):
    slots = sessions.expected_rth_slots(D1)
    rows = [H.bar(s, 5000, source="webull", fetched=s + 310) for i, s in enumerate(slots[:10]) if i != 5]
    H.write_csv(env.root / "fixture.csv", rows)
    env.ingest()
    assert env.scan(H.et(D1, 9, 55, 30))["signal_status"] == "WAIT" and status(env) == (0, None, 1)
    r = env.scan(H.et(D1, 10, 0, 30))                           # 09:55 那根缺失
    assert r["signal_status"] == "DATA_ERROR" and r["new_entries_blocked"] is True
    assert status(env)[0] == 1 and "LATEST_BAR_MISSING" in status(env)[1]
    r2 = env.scan(H.et(D1, 10, 5, 30))                          # 下一根到了：缺口变成内部缺口，仍阻止
    assert r2["problems"][0]["category"] == "UNEXPECTED_GAP" and status(env)[0] == 1
    # 次日数据完整：阻止状态解除，缺口记录保留
    H.write_csv(env.root / "fixture2.csv", [H.bar(s, 5000, source="webull", fetched=s + 310)
                                            for s in sessions.expected_rth_slots(D2)[:4]])
    env.ingest()
    r3 = env.scan(H.et(D2, 9, 50, 30))
    assert r3["signal_status"] == "WAIT" and status(env)[0] == 0 and status(env)[2] == 4
    assert {a[1] for a in cats(env, "UNEXPECTED_GAP")} == {"2025-11-25"}       # 当日进行中的 D2 不算缺口
    d = dict(env.con.execute("SELECT trading_day, reason FROM trading_days").fetchall())
    assert d == {"2025-11-25": "UNEXPECTED_GAP", "2025-11-26": "DAY_IN_PROGRESS"}
    ev = [tuple(r) for r in env.con.execute("SELECT action, cause FROM freeze_events ORDER BY created_at_utc, action")]
    assert sorted(ev) == [("FREEZE", "DATA"), ("UNFREEZE", "DATA")]
    kinds = [r[0] for r in env.con.execute("SELECT kind FROM alerts ORDER BY alert_id")]
    assert kinds == ["DATA_BLOCK", "DATA_BLOCK_CLEARED"]


def test_conflict_blocks_scope(env):
    s0 = sessions.rth_bounds(D1)[0]
    recs = [(111, s0 + 60 * i, 5000, 5001, 4999, 5000, 5) for i in range(390)] + [(111, s0 + 600, 5010, 5011, 5009, 5010, 9)]
    H.write_dbn(env.root / "fixture.dbn", MAP, recs)
    env.ingest()
    r = env.scan(H.et(D1, 12, 0))
    assert r["signal_status"] == "DATA_ERROR" and "CONFLICTING_DUPLICATE" in r["block_reason"]


# ---------------------------------------------------------------- 9.6 快照
def test_snapshot_same_input_same_hash(env):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(D1))
    env.ingest()
    a, b = env.scan(H.et(D1, 12, 0)), env.scan(H.et(D1, 12, 0))
    assert a["snapshot_sha256"] == b["snapshot_sha256"] and a["snapshot_id"] == b["snapshot_id"]
    assert env.con.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 1
    assert env.con.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1          # 重跑不重复
    s = env.con.execute("SELECT * FROM market_snapshots").fetchone()
    assert s["roll_rule_version"] == "roll-v2" and s["calendar_version"] == sessions.CALENDAR_VERSION
    assert s["source"] == "fixsrc" and s["contract_id"] == "ESZ2025" and s["bar_count"] == 29


def test_snapshot_changes_with_visible_revision(env):
    rows = H.rth_bars(D1, source="webull", fetched=H.et(D1, 16, 1))
    H.write_csv(env.root / "fixture.csv", rows)
    env.ingest()
    as_of = H.et(D1, 16, 30)
    a = env.scan(as_of)
    late = dict(rows[3], close="5000.75", fetched_at_utc=sessions.iso(H.et(D1, 16, 10)))    # as_of 时已可见的修订
    future = dict(rows[4], close="5000.75", fetched_at_utc=sessions.iso(H.et(D1, 17, 0)))   # as_of 之后才出现
    H.write_csv(env.root / "fixture.csv", [future], mode="a")
    env.ingest()
    assert env.scan(as_of)["snapshot_sha256"] == a["snapshot_sha256"]                      # 之后的修订不影响
    H.write_csv(env.root / "fixture.csv", [late], mode="a")
    env.ingest()
    assert env.scan(as_of)["snapshot_sha256"] != a["snapshot_sha256"]
