"""本地 TradingView 线 tvfeed：盘中主来源（2026-11-24 起），只读 price-feed 的本地 JSON；Webull 备用。"""
import json
from datetime import date, datetime

import pytest

import helpers as H
from conftest import Env
from trading_agent import live_window as LW
from trading_agent import sessions, tvfeed
from trading_agent.release import manifest as RM

D = date(2025, 11, 25)            # 放行首日
CUT = date(2025, 11, 24)          # 截止日
ROLL = date(2025, 12, 15)         # 2025-12 到期周的星期一：roll-v2 已切到 H2026，连续合约还是 Z2025
CFG = {"source": "tvfeed", "symbols": {"ES1!": "ES"}, "primary_source": "tvfeed"}


def iso_ny(t):
    return datetime.fromtimestamp(t, sessions.NY).isoformat()


def write_feed(root, day, *, fetched, price=5000.0, name="ES1!-1610", extra=()):
    d = root / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    m5 = [{"time": s, "open": price, "high": price + 1, "low": price - 1, "close": price + 0.25, "volume": 10}
          for s in list(sessions.expected_rth_slots(day)) + list(extra)]
    (d / f"{name}.bars.json").write_text(json.dumps({"symbol": "CME_MINI:ES1!", "fetched_at": iso_ny(fetched),
                                                      "m5": m5, "d1": [], "quote": {}}), encoding="utf-8")
    return d


@pytest.fixture
def tenv(tmp_path, monkeypatch):
    monkeypatch.setattr(LW, "load", lambda root: LW.parse({"sources": ["webull", "tvfeed"], "from_day": {"ES": D.isoformat()}}))
    e = Env(tmp_path, dev_visible_through=CUT)
    yield e
    e.con.close()


def run(e, feed, now):
    return tvfeed.collect(e.settings, e.con, now=now, feed_root=feed, cfg=CFG)


def test_local_bars_are_written_as_primary(tenv, tmp_path):
    feed = tmp_path / "feed"
    close = sessions.rth_bounds(D)[1]
    write_feed(feed, D, fetched=close + 60, extra=[close + 300])        # 最后一根取数时没走完
    write_feed(feed, CUT, fetched=close - 86400)                         # 放行首日之前：不读
    r = run(tenv, feed, close + 120)
    assert r["ok"] and r["files_read"] == 1 and r["new_bars"] == 78 and r["unclosed"] >= 1
    row = tenv.con.execute("SELECT source, contract_id, available_at_estimated FROM bars WHERE trading_day=? LIMIT 1",
                           (D.isoformat(),)).fetchone()
    assert tuple(row) == ("tvfeed", "ESZ2025", 0)
    td = tenv.con.execute("SELECT source, complete, degraded FROM trading_days WHERE instrument_root='ES' AND trading_day=?",
                          (D.isoformat(),)).fetchone()
    assert tuple(td) == ("tvfeed", 1, 0)                                 # 主来源：不降级，是有效交易日
    assert run(tenv, feed, close + 180)["existing"] == 78                # 重复读取不重复写


def test_only_standard_contract_lines_are_read(tenv, tmp_path):
    feed = tmp_path / "feed"
    close = sessions.rth_bounds(D)[1]
    write_feed(feed, D, fetched=close + 60)
    write_feed(feed, D, fetched=close + 90, price=6000.0, name="MES1!-1610")   # 旧的微型线：不读
    r = run(tenv, feed, close + 120)
    assert r["files_read"] == 1 and r["new_bars"] == 78
    hi = tenv.con.execute("SELECT MAX(high) FROM bars WHERE trading_day=?", (D.isoformat(),)).fetchone()[0]
    assert hi == (5000 + 1) * 4


def test_local_wins_over_webull(tenv, tmp_path):
    feed = tmp_path / "feed"
    close = sessions.rth_bounds(D)[1]
    write_feed(feed, D, fetched=close + 60)
    rows = H.rth_bars(D, fixture=False, source="webull", contract="ESZ2025")
    H.write_csv(tenv.root / "webull.csv", rows)
    run(tenv, feed, close + 120)
    td = tenv.con.execute("SELECT source, degraded FROM trading_days WHERE instrument_root='ES' AND trading_day=?",
                          (D.isoformat(),)).fetchone()
    assert tuple(td) == ("tvfeed", 0)


def test_roll_week_backup_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setattr(LW, "load", lambda root: LW.parse({"sources": ["tvfeed"], "from_day": {"ES": ROLL.isoformat()}}))
    e = Env(tmp_path, dev_visible_through=CUT)
    try:
        inst = e.settings.instrument("ES")
        assert tvfeed.front_contract(inst, ROLL) == "ESZ2025"
        feed = tmp_path / "feed"
        close = sessions.rth_bounds(ROLL)[1]
        write_feed(feed, ROLL, fetched=close + 60)
        r = run(e, feed, close + 120)
        assert r["roll_window"] == 78 and r["rows_written"] == 0
    finally:
        e.con.close()


def test_local_days_count_for_paper_acceptance(tenv, tmp_path):
    feed = tmp_path / "feed"
    close = sessions.rth_bounds(D)[1]
    write_feed(feed, D, fetched=close + 60)
    run(tenv, feed, close + 120)
    from trading_agent.manual.common import paper_run_id
    run_id = paper_run_id(tenv.scope)
    tenv.con.execute("INSERT INTO engine_runs (run_id, scope, strategy_family, run_mode, risk_domain, state_json,"
                     " created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,0,0)",
                     (run_id, str(tenv.scope), "pa_brooks", "paper", f"paper:{run_id}", "{}"))
    for i, s in enumerate(sessions.expected_rth_slots(D)):
        tenv.con.execute("INSERT INTO scan_events (event_id, scope, strategy_family, run_mode, risk_domain, run_id,"
                         " as_of_utc, snapshot_id, contract_id, signal_status, risk_status, paper_action,"
                         " version_tuple_json, strategy_state_json, decision_json, created_at_utc)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                         (f"e{i}", str(tenv.scope), "pa_brooks", "paper", f"paper:{run_id}", run_id, s + 330, "x",
                          "ESZ2025", "WAIT", "NOT_EVALUATED", "NONE", "{}", "{}",
                          json.dumps({"trading_day": D.isoformat(), "signal_bar_end_utc": s + 300})))
    tenv.con.commit()
    assert RM.paper_run_days(tenv.con, tenv.scope) == [D.isoformat()]    # 本地线是主来源：按时处理的日子算数


def test_tvfeed_is_read_only():
    src = (H.PROJECT / "src" / "trading_agent" / "tvfeed.py").read_text(encoding="utf-8")
    for banned in ("write_text(", "unlink(", "urllib", "requests", "socket", "subprocess"):
        assert banned not in src, banned


def test_tick_warns_when_backup_in_use(tenv, tmp_path, monkeypatch):
    """本地线当天没有 RTH 线、引擎用了 Webull：记 WARN。"""
    import test_engine_runner as ER
    import test_ops as TO
    import test_release as TRL
    import test_research as TR
    from trading_agent.ops import tick as TK
    monkeypatch.setattr(tvfeed, "config", lambda root: CFG)
    t = sessions.rth_bounds(D)[0] + 3600
    rows = [r for r in H.rth_bars(D, fixture=False, source="webull", contract="ESZ2025")
            if sessions.parse_utc(r["bar_start_utc"]) + 300 <= t]
    for r in rows:
        r["fetched_at_utc"] = sessions.iso(t)
    H.write_csv(tenv.root / "webull.csv", rows)
    tenv.ingest()
    assert tvfeed.needs_fallback(tenv.settings, tenv.con, "ES", t) == (True, "本地线今天还没有 RTH 线")
    r = TK.run(tenv.settings, tenv.con, TO.OCFG, registry=TR.REG, release_cfg=TRL.CFG, now=t + 30, shadow=False,
               risk_cfg=ER.RISK, engine_cfg=ER.ENG)
    assert r["scopes"][0]["status"] == "SCANNED"
    kinds = [x[0] for x in tenv.con.execute("SELECT kind FROM alerts")]
    assert "BACKUP_SOURCE_IN_USE" in kinds


def test_needs_fallback(tenv, tmp_path):
    feed = tmp_path / "feed"
    t = sessions.rth_bounds(D)[0] + 3600
    assert tvfeed.needs_fallback(tenv.settings, tenv.con, "ES", t)[0]                 # 还没有本地线
    o = sessions.rth_bounds(D)[0]
    assert tvfeed.needs_fallback(tenv.settings, tenv.con, "ES", o + 57) == (False, "第一根 RTH 线还没收线")  # 09:30 那一轮
    assert tvfeed.needs_fallback(tenv.settings, tenv.con, "ES", o + 300 + 60)[0]      # 09:35 那一轮仍没有本地线：取
    assert tvfeed.needs_fallback(tenv.settings, tenv.con, "ES", sessions.rth_bounds(ROLL)[0] + 57)[0] is False
    write_feed(feed, D, fetched=t)
    run(tenv, feed, t)
    assert tvfeed.needs_fallback(tenv.settings, tenv.con, "ES", t + 60)[0] is False  # 本地线有了：不起 claude 会话
    roll_t = sessions.rth_bounds(ROLL)[0] + 3600
    assert tvfeed.needs_fallback(tenv.settings, tenv.con, "ES", roll_t) == (True, "到期周：不用本地线")
