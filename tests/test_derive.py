import json
from datetime import date

import helpers as H
from trading_agent import context, derive, sessions
from trading_agent.days import FAR_FUTURE

D0, D1, D2 = date(2025, 11, 24), date(2025, 11, 25), date(2025, 11, 26)
MAP = {"ESZ5": [(date(2025, 1, 1), date(2026, 1, 1), 111)]}
DELAY = 5


def m1(day, hh, mm, n=1, base=5000.0, vol=5, step_px=0.25):
    s = H.et(day, hh, mm)
    return [(111, s + 60 * i, base + i * step_px, base + i * step_px + 1, base + i * step_px - 1,
             base + i * step_px + 0.5, vol) for i in range(n)]


def five(env, day, hh, mm, source="databento"):
    return env.con.execute("SELECT * FROM bars WHERE source=? AND timeframe='5m' AND bar_start_utc=? ORDER BY revision",
                           (source, H.et(day, hh, mm))).fetchall()


# ---------------------------------------------------------------- 8.1 五分钟线
def test_full_bucket(env):
    H.write_dbn(env.root / "fixture.dbn", MAP, m1(D1, 9, 30, 5))
    env.ingest()
    (b,) = five(env, D1, 9, 30)
    assert (b["open"], b["close"], b["high"], b["low"], b["volume"]) == (20000, 20006, 20008, 19996, 25)
    assert b["constituent_count"] == 5 and b["derived"] == 1 and b["session_type"] == "RTH"
    assert b["bar_end_utc"] == H.et(D1, 9, 35) and b["available_at_utc"] == H.et(D1, 9, 35) + DELAY
    assert json.loads(b["deps_json"]) == [[H.et(D1, 9, 30) + 60 * i, 1] for i in range(5)]


def test_partial_and_empty_bucket(env):
    recs = m1(D1, 9, 30, 1) + m1(D1, 9, 32, 1) + m1(D1, 9, 34, 1) + m1(D1, 9, 40, 5)     # 09:35 整段无成交
    H.write_dbn(env.root / "fixture.dbn", MAP, recs)
    env.ingest()
    (b,) = five(env, D1, 9, 30)
    assert b["constituent_count"] == 3
    assert five(env, D1, 9, 35) == []                                                       # 不生成、不插值
    assert len(five(env, D1, 9, 40)) == 1


def test_bucket_does_not_cross_session(env):
    H.write_dbn(env.root / "fixture.dbn", MAP, m1(D1, 9, 25, 10))
    env.ingest()
    (a,), (b,) = five(env, D1, 9, 25), five(env, D1, 9, 30)
    assert a["session_type"] == "ETH" and b["session_type"] == "RTH"
    assert [x[0] for x in json.loads(a["deps_json"])] == [H.et(D1, 9, 25) + 60 * i for i in range(5)]
    assert [x[0] for x in json.loads(b["deps_json"])] == [H.et(D1, 9, 30) + 60 * i for i in range(5)]


def test_native_5m_is_not_aggregated(env):
    rows = H.rth_bars(D1, source="webull", fetched=H.et(D1, 16, 1))
    H.write_csv(env.root / "fixture_wb.csv", rows)
    env.ingest()
    assert env.count("timeframe='5m' AND derived=1") == 0
    got = env.con.execute("SELECT open, high, low, close, volume, constituent_count FROM bars WHERE timeframe='5m'"
                          " ORDER BY bar_start_utc").fetchall()
    assert len(got) == 78 and got[0]["constituent_count"] is None
    assert got[0]["open"] == 20000 and got[0]["close"] == 20002


# ---------------------------------------------------------------- 8.2 一小时线
def test_hourly_only_when_finished(env):
    H.write_csv(env.root / "fixture.csv", H.eth_bars(D1) + H.rth_bars(D1))
    env.ingest()
    rows = context.hourly_asof(env.con, "fixsrc", "ESZ2025", H.et(D1, 10, 40))
    assert rows[-1]["bar_start_utc"] == H.et(D1, 9, 0) and rows[-1]["bar_end_utc"] == H.et(D1, 10, 0)
    assert rows[-1]["session_type"] == "MIX" and rows[-1]["constituent_count"] == 12
    assert all(r["bar_end_utc"] <= H.et(D1, 10, 40) for r in rows)
    later = context.hourly_asof(env.con, "fixsrc", "ESZ2025", H.et(D1, 11, 0) + DELAY)
    assert later[-1]["bar_start_utc"] == H.et(D1, 10, 0)


def test_hour_with_gap_not_built(env):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(D1, skip=[(10, 15)]))
    env.ingest()
    starts = {r[0] for r in env.con.execute("SELECT bar_start_utc FROM bars WHERE timeframe='1h'")}
    assert H.et(D1, 10, 0) not in starts and H.et(D1, 11, 0) in starts
    a = env.con.execute("SELECT start_utc, detail FROM data_anomalies WHERE category='INCOMPLETE_HOUR'").fetchall()
    assert [r[0] for r in a] == [H.et(D1, 10, 0)]


# ---------------------------------------------------------------- 8.3 RTH 日高低差
def test_rth_ranges_exclude_current_day(env):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(D0) + H.rth_bars(D1) + H.rth_bars(D2))
    env.ingest()
    days = context.rth_day_ranges_asof(env.con, env.settings, "ES", H.et(D2, 11, 0), n=3)
    assert [d["trading_day"] for d in days][:2] == ["2025-11-25", "2025-11-24"]
    assert all(d["trading_day"] < "2025-11-26" for d in days)
    assert days[0]["complete"] and days[0]["range_ticks"] == days[0]["high"] - days[0]["low"] > 0
    assert days[2]["complete"] is False and days[2]["reason"] == "NO_DATA"       # 2025-11-21 没有数据


def test_day_with_gap_is_not_a_complete_day(env):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(D1, skip=[(11, 0), (11, 5)]) + H.rth_bars(D2))
    env.ingest()
    r = env.con.execute("SELECT complete, reason FROM rth_days WHERE trading_day='2025-11-25'").fetchone()
    assert tuple(r) == (0, "UNEXPECTED_GAP:2")
    days = context.rth_day_ranges_asof(env.con, env.settings, "ES", H.et(D2, 16, 30), n=1)
    assert days[0]["complete"] is False and days[0]["reason"].startswith("UNEXPECTED_GAP")


# ---------------------------------------------------------------- 8.4 参考价位
def test_reference_levels_regular_day(env):
    H.write_csv(env.root / "fixture.csv",
                H.rth_bars(D1, base=5000) + H.eth_bars(D2, base=4990) + H.rth_bars(D2, base=5010))
    env.ingest()
    lv = env.con.execute("SELECT * FROM reference_levels WHERE trading_day='2025-11-26'").fetchone()
    d1 = env.con.execute("SELECT high, low, close FROM rth_days WHERE trading_day='2025-11-25'").fetchone()
    assert (lv["pdh"], lv["pdl"], lv["prev_rth_close"]) == tuple(d1) and lv["prev_day_status"] == "OK"
    on = env.con.execute("SELECT MAX(high), MIN(low) FROM bars WHERE trading_day='2025-11-26' AND session_type='ETH'"
                         " AND timeframe='5m' AND bar_start_utc<?", (H.et(D2, 9, 25),)).fetchone()
    assert (lv["onh"], lv["onl"]) == tuple(on) and lv["overnight_status"] == "OK"
    # 计算时点是 RTH 开盘：09:25–09:30 那根要到 09:30:05 才可见，所以不在其中
    assert lv["as_of_utc"] == H.et(D2, 9, 30) and lv["known_at_utc"] == H.et(D2, 9, 25) + DELAY
    deps = json.loads(lv["deps_json"])
    assert deps["prev_day"]["trading_day"] == "2025-11-25" and len(deps["prev_day"]["bars"]) == 78
    assert len(deps["overnight"]) == 77 and all(rev == 1 for _, rev in deps["overnight"])


def test_reference_levels_on_roll_day(env):
    # roll-v1：2025-12-11 起实际合约为 ESH2026。前一日只有 ESZ2025 的数据。
    before, roll = date(2025, 12, 10), date(2025, 12, 11)
    H.write_csv(env.root / "fixture.csv",
                H.rth_bars(before, contract="ESZ2025") + H.rth_bars(roll, contract="ESH2026"))
    env.ingest()
    lv = env.con.execute("SELECT * FROM reference_levels WHERE trading_day='2025-12-11'").fetchone()
    assert lv["contract_id"] == "ESH2026" and lv["pdh"] is None and lv["prev_rth_close"] is None
    assert lv["prev_day_status"] == "UNAVAILABLE:CONTRACT_ROLLED_NO_PRIOR_DATA"


def test_reference_levels_on_roll_day_when_new_contract_traded_before(env):
    before, roll = date(2025, 12, 10), date(2025, 12, 11)
    H.write_csv(env.root / "fixture.csv",
                H.rth_bars(before, contract="ESZ2025", base=5000) + H.rth_bars(before, contract="ESH2026", base=5050)
                + H.rth_bars(roll, contract="ESH2026", base=5060))
    env.ingest()
    lv = env.con.execute("SELECT * FROM reference_levels WHERE trading_day='2025-12-11'").fetchone()
    assert lv["prev_day_status"] == "OK" and lv["pdl"] == (5050 - 1) * 4      # 取自新合约，不是旧合约


# ---------------------------------------------------------------- 8.5 因果可见性
def test_bar_not_visible_before_known_at(env):
    H.write_dbn(env.root / "fixture.dbn", MAP, m1(D1, 9, 30, 5))
    env.ingest()
    known = H.et(D1, 9, 35) + DELAY
    q = lambda t: context.visible_bars(env.con, "databento", "ESZ2025", "5m", t, trading_day="2025-11-25")   # noqa: E731
    assert q(known - 3) == [] and len(q(known)) == 1


def test_late_revision_does_not_change_earlier_view(env):
    s0 = H.et(D1, 9, 30)
    t1, t2 = s0 + 310, H.et(D1, 12, 0)
    rows = [H.bar(s0 + 300 * i, 5000, source="webull", fetched=s0 + 300 * (i + 1) + 10) for i in range(12)]
    rows.append(H.bar(s0, 5005, source="webull", fetched=t2))               # 09:30 那根在 12:00 才被修订
    H.write_csv(env.root / "fixture.csv", rows)
    env.ingest()
    q = lambda t: context.visible_bars(env.con, "webull", "ESZ2025", "5m", t, trading_day="2025-11-25")[0]   # noqa: E731
    assert (q(t1)["revision"], q(t1)["open"]) == (1, 20000)
    assert (q(t2 - 1)["revision"], q(t2 - 1)["open"]) == (1, 20000)
    assert (q(t2)["revision"], q(t2)["open"]) == (2, 20020)
    # 依赖它的 1 小时线也有两个版本：修订之前看到的是旧版本
    h = env.con.execute("SELECT revision, open, available_at_utc, deps_json FROM bars WHERE timeframe='1h'"
                        " AND bar_start_utc=? ORDER BY revision", (H.et(D1, 9, 0),)).fetchall()
    assert [(r["revision"], r["open"]) for r in h] == [(1, 20000), (2, 20020)]
    assert h[0]["available_at_utc"] == H.et(D1, 10, 0) + 10 and h[1]["available_at_utc"] == t2
    assert json.loads(h[1]["deps_json"])[0] == [s0, 2]
    hv = context.hourly_asof(env.con, "webull", "ESZ2025", t2 - 1)
    assert hv[0]["open"] == 20000


def test_backfilled_bar_invisible_until_fetched(env):
    s0 = H.et(D1, 10, 0)
    back = H.et(D1, 11, 20)
    H.write_csv(env.root / "fixture.csv", [H.bar(s0, 5000, source="webull", fetched=back)])
    env.ingest()
    q = lambda t: context.visible_bars(env.con, "webull", "ESZ2025", "5m", t, trading_day="2025-11-25")   # noqa: E731
    assert q(H.et(D1, 10, 30)) == [] and q(back - 1) == [] and len(q(back)) == 1
    assert q(back)[0]["available_at_estimated"] == 0


# ---------------------------------------------------------------- 8.6 可重现
def test_derived_rebuild_is_identical(env):
    from trading_agent import days
    H.write_dbn(env.root / "fixture.dbn", MAP, m1(D1, 9, 30, 390) + m1(D2, 9, 30, 200))
    H.write_csv(env.root / "fixture.csv", H.eth_bars(D0) + H.rth_bars(D0, skip=[(13, 0)]))
    env.ingest()
    dump = lambda: {t: [tuple(r) for r in env.con.execute(f"SELECT * FROM {t} ORDER BY 1,2,3,4,5")]   # noqa: E731
                    for t in ("bars", "rth_days", "reference_levels", "trading_days")}
    anomalies = lambda: sorted(tuple(r)[1:10] for r in env.con.execute("SELECT * FROM data_anomalies"))   # noqa: E731
    before, a_before = dump(), anomalies()
    assert env.count("derived=1") > 0
    derive.clear_derived(env.con)
    assert env.count("derived=1") == 0
    affected = derive.rebuild(env.con, env.settings, derive.all_raw_keys(env.con))
    days.refresh(env.con, env.settings, affected)
    assert dump() == before and anomalies() == a_before
