import json
import re
from datetime import date

import helpers as H
from trading_agent import report, sessions

START = date(2025, 9, 15)       # ESZ2025 作为实际合约的区间内


def days_from(start, n):
    return sessions.trading_days(start, date(2026, 12, 31))[:n]


def real(day, **kw):
    kw.setdefault("source", "databento")
    kw.setdefault("contract", "ESZ2025")
    return H.rth_bars(day, fixture=False, **kw)


def data(env, cutoff=None):
    return report.collect(env.settings, env.con, cutoff)


# ---------------------------------------------------------------- 11.1 按实际合约汇总
def test_coverage_numbers(env):
    d = days_from(START, 3)
    rows = real(d[0]) + real(d[1], skip=[(10, 0), (10, 5), (14, 0)]) + real(d[2])
    rows += [H.bar(H.et(d[2], 4, 0) + 300 * i, 4990, fixture=False, source="databento") for i in range(6)]   # 夜盘
    H.write_csv(env.root / "real.csv", rows)
    extra = dict(rows[0], close="5000.75")
    H.write_csv(env.root / "real2.csv", [extra])                    # 一根修订
    env.ingest()
    r = data(env)
    es = r["roots"]["ES"]
    assert (es["first_day"], es["last_day"], es["complete_days"]) == (d[0].isoformat(), d[2].isoformat(), 2)
    assert es["gap_days"] == [(d[1].isoformat(), 2, 3)] and es["degraded_days"] == []
    (c,) = es["contracts"]
    assert c["contract_id"] == "ESZ2025" and c["bars"] == 78 * 3 - 3 + 6 + 1
    assert (c["rth_days"], c["eth_days"], c["active_days"], c["active_complete"], c["active_gaps"]) == (3, 1, 3, 2, 2)
    assert c["estimated_pct"] == 100.0 and c["revised"] == 1
    assert c["first_bar"] == sessions.iso(H.et(d[0], 9, 30)) and c["last_bar"] == sessions.iso(H.et(d[2], 16, 0))
    (s,) = r["sources"]
    assert (s["source"], s["timeframe"], s["days"], s["revised"]) == ("databento", "5m", 3, 1)
    text = env.report.read_text(encoding="utf-8")
    assert "| ESZ2025 |" in text and "NO_REAL_DATA" not in text


# ---------------------------------------------------------------- 11.2 来源衔接
def test_sources_gap_and_overlap(env):
    d = days_from(START, 7)
    a = real(d[0]) + real(d[1]) + real(d[2])
    wb = lambda day: real(day, source="webull", fetched=H.et(day, 16, 1))   # noqa: E731
    b = wb(d[2]) + wb(d[5]) + wb(d[6])
    b[3] = dict(b[3], close="5000.75")
    b[9] = dict(b[9], high="5010")
    H.write_csv(env.root / "real_a.csv", a)
    H.write_csv(env.root / "real_b.csv", b)
    env.ingest()
    es = data(env)["roots"]["ES"]
    assert es["uncovered"] == [{"from": d[3].isoformat(), "to": d[4].isoformat(), "trading_days": 2}]
    assert es["overlap"] == {"trading_days": 1, "bars_compared": 78, "bars_different": 2}
    assert es["days_by_source"] == {"databento": 3, "webull": 2}
    assert [(s["from"], s["to"]) for s in es["segments"]] == [(d[0].isoformat(), d[2].isoformat()),
                                                              (d[5].isoformat(), d[6].isoformat())]
    text = env.report.read_text(encoding="utf-8")
    assert f"| {d[3].isoformat()} | {d[4].isoformat()} | 2 |" in text and "开高低收不一致 2 根" in text


# ---------------------------------------------------------------- 11.3 换月核对
def test_roll_rule_vs_volume_cross(env):
    days = sessions.trading_days(date(2025, 12, 8), date(2025, 12, 17))
    rows = []
    for d in days:
        new_leads = d >= date(2025, 12, 16)
        rows += real(d, contract="ESZ2025", volume=10 if new_leads else 100)
        rows += real(d, contract="ESH2026", volume=100 if new_leads else 10)
    H.write_csv(env.root / "real.csv", rows)
    env.ingest()
    (roll,) = data(env)["roots"]["ES"]["rolls"]
    assert roll == {"rule_date": "2025-12-15", "old": "ESZ2025", "new": "ESH2026", "rule_version": "roll-v2",
                    "volume_cross": "2025-12-16", "cross_minus_rule_days": 1, "days_compared": len(days)}
    assert "| 2025-12-15 | ESZ2025 | ESH2026 | roll-v2 | 2025-12-16 | 1 |" in env.report.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 11.4 / 11.5 预热与窗口
def build(env, n, *, degraded=(), hole=(), with_eth=True):
    d = days_from(START, n)
    rows = []
    for i, day in enumerate(d):
        if i in hole:
            continue
        rows += real(day)
        if with_eth:
            rows += [H.bar(H.et(day, 3, 0) + 300 * k, 4990, fixture=False, source="databento") for k in range(24)]
    H.write_csv(env.root / "real.csv", rows)
    if degraded:
        (env.root / "condition.json").write_text(json.dumps(
            [{"date": d[i].isoformat(), "condition": "degraded"} for i in degraded]))
    env.ingest()
    return d


def test_warmup(env):
    d = build(env, 13)
    (seg,) = data(env)["roots"]["ES"]["segments"]
    assert seg["warmup_short_days"] == 10 and seg["warm_from"] == d[10].isoformat() and seg["valid_days"] == 3


def test_warmup_restarts_after_uncovered_stretch(env):
    d = build(env, 26, hole=(12,))
    segs = data(env)["roots"]["ES"]["segments"]
    assert [(s["from"], s["warm_from"], s["warmup_short_days"]) for s in segs] == [
        (d[0].isoformat(), d[10].isoformat(), 10), (d[13].isoformat(), d[23].isoformat(), 10)]


def test_window_counts(env):
    build(env, 30)
    es = data(env)["roots"]["ES"]
    assert es["segments"][0]["valid_days"] == 20 and (es["capsules_20"], es["confirm_40"]) == (1, 0)


def test_window_counts_drop_degraded_day(env):
    build(env, 30, degraded=(15,))
    es = data(env)["roots"]["ES"]
    assert es["degraded_days"] and es["segments"][0]["valid_days"] == 19 and es["capsules_20"] == 0


def test_window_counts_do_not_span_uncovered_stretch(env):
    build(env, 31, hole=(20,))                 # 30 个有数据的交易日，但被空档切成两段，各自重新预热
    es = data(env)["roots"]["ES"]
    assert [s["valid_days"] for s in es["segments"]] == [10, 0] and es["capsules_20"] == 0


def test_window_counts_50_days(env):
    build(env, 50, with_eth=False)             # 没有夜盘时 1 小时线也够（每个 RTH 日 6 根）
    es = data(env)["roots"]["ES"]
    assert es["segments"][0]["valid_days"] == 40 and (es["capsules_20"], es["confirm_40"]) == (2, 1)


# ---------------------------------------------------------------- 11.6 保留边界与 NO_REAL_DATA
PRICE = re.compile(r"\b[45]\d{3}(\.\d+)?\b")


def test_cutoff_reports_coverage_only(tmp_path):
    from conftest import Env
    e = Env(tmp_path)
    d = days_from(START, 4)
    H.write_csv(e.root / "real.csv", real(d[0]) + real(d[1]) + real(d[2], skip=[(12, 0)]) + real(d[3]))
    (e.root / "condition.json").write_text(json.dumps([{"date": d[3].isoformat(), "condition": "degraded"}]))
    e.ingest(through=d[1])
    r = data(e, d[1])
    assert r["flags"] == [] and r["cutoff"] == d[1].isoformat()
    assert [(w["trading_day"], w["bars"], w["rth_slots_missing"], w["degraded"]) for w in r["withheld_days"]] == [
        (d[2].isoformat(), 77, 1, False), (d[3].isoformat(), 78, 0, True)]
    text = e.report.read_text(encoding="utf-8")
    sec = text.split("## 截止日之后")[1].split("\n## ")[0]
    assert "实际合约有数据的交易日：2" in sec and "有缺失的交易日：1" in sec and "降级日：1" in sec
    assert not PRICE.search(sec.replace(d[2].isoformat(), "").replace(d[3].isoformat(), ""))
    assert r["roots"]["ES"]["last_day"] == d[1].isoformat() and "HOLDOUT_BOUNDARY_NOT_SET" not in text
    e.con.close()


def test_no_cutoff_flag(env):
    H.write_csv(env.root / "real.csv", real(START))
    env.ingest()
    assert "HOLDOUT_BOUNDARY_NOT_SET" in data(env)["flags"]
    assert env.report.read_text(encoding="utf-8").splitlines()[4].startswith("> **HOLDOUT_BOUNDARY_NOT_SET**")


def test_fixture_only_is_no_real_data(env):
    H.write_csv(env.root / "fixture.csv", sum((H.rth_bars(d) for d in days_from(START, 35)), []))
    env.ingest()
    r = data(env)
    assert "NO_REAL_DATA" in r["flags"] and r["roots"] == {} and r["real_bars"] == 0 and r["fixture_bars"] == 78 * 35
    text = env.report.read_text(encoding="utf-8")
    assert text.splitlines()[4].startswith("> **NO_REAL_DATA**")
    assert "可封存的 20 日胶囊：**0**" in text and "连续 40 日确认段：**0**" in text and "真实行情还缺什么" in text


# ---------------------------------------------------------------- 11.7 可重现
def test_report_is_reproducible(env):
    build(env, 14, degraded=(3,))
    a = env.report.read_text(encoding="utf-8")
    assert env.ingest()["files_skipped"] == 2
    b = env.report.read_text(encoding="utf-8")
    assert report.body(a) == report.body(b)
    d1, d2 = data(env), data(env)
    assert d1 == d2
    assert report.render(env.settings, d1, 1) != report.render(env.settings, d1, 2)       # 只有生成时间不同
    assert report.body(report.render(env.settings, d1, 1)) == report.body(report.render(env.settings, d1, 2))
