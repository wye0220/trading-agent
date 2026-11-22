from datetime import date
from decimal import Decimal

import pytest
import yaml

import helpers as H
from trading_agent import contracts, sessions
from trading_agent.config import ConfigError, load_collect, load_settings, load_watch, parse_scope


def test_instrument_specs(env):
    want = {"ES": (50, "12.5"), "NQ": (20, "5"), "MES": (5, "1.25"), "MNQ": (2, "0.5")}
    for root, (pv, tv) in want.items():
        inst = env.settings.instrument(root)
        assert inst.point_value_usd == pv and inst.tick_size == Decimal("0.25")
        assert inst.tick_value_usd == Decimal(tv)
    with pytest.raises(ConfigError, match="品种未登记"):
        env.settings.instrument("CL")


def test_roll_v2_dates_2025(env):
    es = env.settings.instrument("ES")
    assert all(env.settings.instrument(r).roll_rule_version == "roll-v2" for r in ("ES", "NQ", "MES", "MNQ"))
    rule = env.settings.roll_rules["roll-v2"]
    # 到期日前第 6 个日历日是星期六，实际从紧接着的星期一交易日起换月
    assert [contracts.roll_date(c, rule) for c in ("ESH2025", "ESM2025", "ESU2025", "ESZ2025")] == [
        date(2025, 3, 15), date(2025, 6, 14), date(2025, 9, 13), date(2025, 12, 13)]
    active = lambda d: contracts.active_contract(es, d, env.settings.roll_rules)   # noqa: E731
    assert active(date(2025, 3, 14)) == "ESH2025" and active(date(2025, 3, 17)) == "ESM2025"
    assert active(date(2025, 6, 13)) == "ESM2025" and active(date(2025, 6, 16)) == "ESU2025"
    assert active(date(2025, 9, 12)) == "ESU2025" and active(date(2025, 9, 15)) == "ESZ2025"
    assert active(date(2025, 12, 12)) == "ESZ2025" and active(date(2025, 12, 15)) == "ESH2026"
    assert [(d.isoformat(), o, n) for d, o, n in contracts.roll_schedule(
        es, date(2025, 12, 1), date(2025, 12, 31), env.settings.roll_rules)] == [("2025-12-13", "ESZ2025", "ESH2026")]


def test_roll_v1_still_defined(env):
    rule = env.settings.roll_rules["roll-v1"]
    assert contracts.roll_date("ESH2025", rule) == date(2025, 3, 13)


def test_contract_year_resolution():
    assert contracts.normalize("ESH0", date(2019, 9, 5)) == "ESH2020"
    assert contracts.normalize("ESH0", date(2024, 12, 8)) == "ESH2030"
    assert contracts.normalize("ESZ9", date(2019, 9, 5)) == "ESZ2019"
    assert contracts.normalize("ESZ2026", date(2026, 1, 1)) == "ESZ2026"
    assert contracts.short_symbol("ESZ2026") == "ESZ6"
    assert contracts.is_spread("ESH0-ESM0")
    with pytest.raises(contracts.ContractError):
        contracts.normalize("ESH0-ESM0", date(2020, 1, 1))


def test_paths_must_be_outside_project(tmp_path):
    with pytest.raises(ConfigError, match="数据根目录必须在项目之外"):
        load_settings(H.PROJECT, env={}, data_root=H.PROJECT / "data")
    with pytest.raises(ConfigError, match="数据根目录必须在项目之外"):
        load_settings(H.PROJECT, env={"TRADING_AGENT_DATA_ROOT": str(H.PROJECT / "data")})
    s = load_settings(H.PROJECT, env={"TRADING_AGENT_DATA_ROOT": str(tmp_path / "x")})
    assert s.data_root == (tmp_path / "x").resolve()
    assert s.runtime_db == (tmp_path / "x").resolve() / "runtime" / "trading_agent.sqlite"
    assert H.PROJECT.resolve() not in s.holdout_root.parents and s.holdout_root != H.PROJECT.resolve()


def test_holdout_inside_project_rejected(tmp_path):
    proj = tmp_path / "proj"
    (proj / "config").mkdir(parents=True)
    for f in ("instruments.yaml", "watch.yaml", "collect.yaml"):
        (proj / "config" / f).write_text((H.PROJECT / "config" / f).read_text(encoding="utf-8"), encoding="utf-8")
    paths = yaml.safe_load((H.PROJECT / "config" / "paths.yaml").read_text(encoding="utf-8"))
    paths["holdout_root"] = str(proj / "holdout")
    (proj / "config" / "paths.yaml").write_text(yaml.safe_dump(paths), encoding="utf-8")
    with pytest.raises(ConfigError, match="保留集路径必须在项目之外"):
        load_settings(proj, env={}, data_root=tmp_path / "d")


def test_watch_config(tmp_path):
    w = load_watch(H.PROJECT / "config" / "watch.yaml")
    assert w == {"poll_interval_seconds": 60, "presets": [60, 300], "stream_first": True}
    p = tmp_path / "w.yaml"
    p.write_text("poll_interval_seconds: 17\n")
    assert load_watch(p)["poll_interval_seconds"] == 17
    for bad in ("0", "-5", "1.5", "true"):
        p.write_text(f"poll_interval_seconds: {bad}\n")
        with pytest.raises(ConfigError):
            load_watch(p)


def test_collect_config(tmp_path):
    c = load_collect(H.PROJECT / "config" / "collect.yaml")
    assert c["timeframe"] == "5m" and c["lookback_trading_days"] == 2 and c["wait_after_close_seconds"] == 10
    assert c["include_eth"] is True
    p = tmp_path / "c.yaml"
    p.write_text("{}\n")
    assert load_collect(p)["timeframe"] == "5m"
    for bad in ("timeframe: 15m", "lookback_trading_days: 0", "wait_after_close_seconds: -1",
                "max_bars_per_request: 5000"):
        p.write_text(bad + "\n")
        with pytest.raises(ConfigError):
            load_collect(p)


def test_scope_parsing(env):
    s = parse_scope("pa_brooks:ES:RTH:5m", env.settings)
    assert str(s) == "pa_brooks:ES:RTH:5m"
    for bad in ("ES:5m", "pa_brooks:CL:RTH:5m", "pa_brooks:ES:XXX:5m", "pa_brooks:ES:RTH:", "pa_brooks:ES:RTH:15m"):
        with pytest.raises(ConfigError):
            parse_scope(bad, env.settings)


# ---------------------------------------------------------------- 交易日历
def test_calendar_regular_day():
    d = date(2025, 11, 25)
    o, c = sessions.rth_bounds(d)
    assert (o, c) == (H.et(d, 9, 30), H.et(d, 16, 0))
    assert len(sessions.expected_rth_slots(d)) == 78 and not sessions.is_early_close(d)
    assert sessions.classify(H.et(d, 9, 30)) == (d, "RTH")
    assert sessions.classify(H.et(d, 9, 25)) == (d, "ETH")
    assert sessions.classify(H.et(d, 15, 55)) == (d, "RTH")
    assert sessions.classify(H.et(d, 16, 0)) == (d, "ETH")
    assert sessions.classify(H.et(d, 17, 30)) == (None, "OUTSIDE")           # 每日维护时段
    assert sessions.classify(H.et(d, 18, 0)) == (date(2025, 11, 26), "ETH")   # 18:00 起属于下一交易日
    assert sessions.eth_bounds(d) == (H.et(date(2025, 11, 24), 18), H.et(d, 17))
    assert "exchange_calendars" in sessions.CALENDAR_VERSION


def test_calendar_half_day():
    d = date(2025, 11, 28)
    assert sessions.is_early_close(d)
    assert sessions.rth_bounds(d)[1] == H.et(d, 13, 0) and len(sessions.expected_rth_slots(d)) == 42
    assert sessions.classify(H.et(d, 12, 55)) == (d, "RTH")
    assert sessions.classify(H.et(d, 13, 0)) == (d, "ETH")


def test_calendar_dst_weeks():
    # 夏令时 2025-03-09 开始、2025-11-02 结束：RTH 始终是纽约时间 09:30–16:00
    for d, utc_open_hour in ((date(2025, 3, 7), 14), (date(2025, 3, 10), 13), (date(2025, 10, 31), 13),
                             (date(2025, 11, 3), 14)):
        o, c = sessions.rth_bounds(d)
        assert sessions.et(o).strftime("%H:%M") == "09:30" and sessions.et(c).strftime("%H:%M") == "16:00"
        assert (o % 86400) // 3600 == utc_open_hour
        assert len(sessions.expected_rth_slots(d)) == 78
        assert sessions.classify(o) == (d, "RTH") and sessions.classify(o - 300) == (d, "ETH")
    # 切换当晚的周日夜盘属于周一交易日
    assert sessions.classify(H.et(date(2025, 3, 9), 18, 0)) == (date(2025, 3, 10), "ETH")


def test_calendar_holiday_and_weekend():
    hol = date(2025, 11, 27)                                     # 感恩节
    assert not sessions.is_trading_day(hol)
    assert sessions.next_trading_day(date(2025, 11, 26)) == date(2025, 11, 28)
    assert sessions.prev_trading_day(date(2025, 11, 28)) == date(2025, 11, 26)
    # 休市日的 Globex 交易归入下一交易日的 ETH 背景
    assert sessions.classify(H.et(hol, 10, 0)) == (date(2025, 11, 28), "ETH")
    assert sessions.classify(H.et(date(2025, 11, 29), 10, 0)) == (None, "OUTSIDE")   # 周六
    assert sessions.trading_days(date(2025, 11, 24), date(2025, 11, 28)) == [
        date(2025, 11, 24), date(2025, 11, 25), date(2025, 11, 26), date(2025, 11, 28)]
