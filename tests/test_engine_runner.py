import json
import re
from datetime import date

import pytest

import helpers as H
import test_strategy_spi as SPI
from trading_agent import sessions
from trading_agent.engine import config as EC, fills, runner as RN, versions
from trading_agent.strategies.base import StrategyState
from trading_agent.strategies.pa_brooks import PaBrooksStrategy

D1, D2 = date(2025, 11, 25), date(2025, 11, 26)
PA = PaBrooksStrategy.from_default_config()
MOCK = SPI.MockStrategy()
RISK = EC.load_risk(H.PROJECT)                 # 费用未配置，止损滑点 1 tick
ENG = EC.load_engine(H.PROJECT)

# 一个 RTH 交易日的脚本（点数）。10:00 起：Spike → 回调 → H1 成交后止损 → H2 成交后止损 → H3 被停手拦截。
SCRIPT = [
    (5000, 5001, 4999, 5000.25),          # 0  10:00
    (5000.25, 5006, 5000, 5005.75),       # 1  强阳线
    (5005.75, 5012, 5005.5, 5011.5),      # 2  Spike 确认，极值 5012
    (5011.5, 5011.75, 5009, 5009.5),      # 3  回调第 1 根
    (5009.5, 5010, 5007, 5007.5),         # 4  反向腿
    (5007.5, 5010, 5007.25, 5009.75),     # 5  H1：触发 5010.25，限价 5010.5，止损 5006.75
    (5009.75, 5012, 5009.5, 5011.5),      # 6  成交于 5010.25
    (5011.5, 5011.75, 5006, 5006.5),      # 7  止损（5006.75 - 1 tick 滑点 = 5006.5）；同时开启新腿
    (5006.5, 5009, 5006.25, 5008.75),     # 8  H2：触发 5009.25，限价 5009.5，止损 5005.75
    (5008.75, 5010, 5008.5, 5009.5),      # 9  成交于 5009.25
    (5009.5, 5009.75, 5005, 5005.5),      # 10 止损（5005.5）；第二次止损；开启新腿
    (5005.5, 5008, 5005.25, 5007.75),     # 11 H3 形态成立，但已两次止损 → TWO_STOPS_HALT
    (5007.75, 5008, 5007, 5007.5),        # 12
]
T0 = (10, 0)


def scripted_day(day=D1, *, source="fixsrc", script=SCRIPT, fetched_lag=None, one_minute=False,
                 default=(5007.5, 5008, 5007, 5007.5)):
    """夜盘 + 开盘后的平盘（给 ATR 预热）+ 10:00 起的脚本 + 之后平盘到收盘。"""
    rows = H.eth_bars(day, base=5000, source=source)
    start = H.et(day, *T0)
    for s in sessions.expected_rth_slots(day):
        i = (s - start) // 300
        o, h, l, c = script[i] if 0 <= i < len(script) else default
        fetched = None if fetched_lag is None else s + 300 + fetched_lag
        if not one_minute:
            rows.append(H.bar(s, o, high=h, low=l, close=c, source=source, fetched=fetched))
        else:                                   # 5 根 1 分钟线：先到低点，再到高点，最后回到收盘
            for k, (mo, mh, ml, mc) in enumerate([(o, o, o, o), (o, o, l, l), (l, h, l, h), (h, h, c, c), (c, c, c, c)]):
                rows.append(H.bar(s + 60 * k, mo, high=mh, low=ml, close=mc, tf="1m", source=source))
    if one_minute:
        rows = [r for r in rows if r["timeframe"] == "1m"] + [
            dict(r, timeframe="1m") for r in H.eth_bars(day, base=5000, source=source, step=60)]
        rows = [r for r in rows if r["timeframe"] == "1m"]
    return rows


def load(env, rows, name="fixture_day.csv"):
    H.write_csv(env.root / name, rows)
    env.ingest()


def bar_end(i, day=D1):
    return H.et(day, *T0) + 300 * (i + 1)


def do_replay(env, strategy=PA, start=D1, end=D1, **kw):
    kw.setdefault("risk_cfg", RISK)
    kw.setdefault("engine_cfg", ENG)
    return RN.replay(env.settings, env.con, env.scope, strategy, start, end, **kw)


def rows(env, sql, *args):
    return [dict(r) for r in env.con.execute(sql, args)]


def px(points):
    return int(points * 4)


# ---------------------------------------------------------------- 6.2 / 6.9 PA 跑通
def test_pa_replay_end_to_end(env):
    load(env, scripted_day())
    out = do_replay(env)
    run_id = out["run_id"]
    assert out["run_mode"] == "fixture" and out["steps"] == 78 and out["setups"] == 2 and out["orders"] == 2
    assert out["trades"] == 2

    orders = rows(env, "SELECT * FROM paper_orders WHERE run_id=? ORDER BY armed_at_utc", run_id)
    assert [(o["status"], o["side"], o["order_type"], o["quantity"]) for o in orders] == [
        ("FILLED", "LONG", "STOP_LIMIT_NEXT_BAR", 1)] * 2
    o1, o2 = orders
    assert (o1["trigger_ticks"], o1["limit_ticks"], o1["stop_ticks"]) == (px(5010.25), px(5010.5), px(5006.75))
    assert (o1["signal_bar_end_utc"], o1["armed_at_utc"], o1["expires_at_utc"]) == (
        bar_end(5), bar_end(5) + 5, bar_end(6))                       # 可用时点激活，下一根收盘过期
    assert (o2["trigger_ticks"], o2["stop_ticks"]) == (px(5009.25), px(5005.75))
    assert json.loads(o1["target_json"]) == {"kind": "INITIAL_RISK_MULTIPLE", "value": 2}
    assert json.loads(o1["flags_json"]) == ["AMBIGUOUS_ENTRY"]        # 只有 5 分钟线：下一根跨着激活时刻

    trades = rows(env, "SELECT * FROM paper_trades WHERE run_id=? ORDER BY exit_time_utc", run_id)
    t1, t2 = trades
    assert (t1["entry_ticks"], t1["exit_ticks"], t1["exit_reason"], t1["pnl_ticks"]) == (
        px(5010.25), px(5006.5), "STOP", -15)
    assert (t2["entry_ticks"], t2["exit_ticks"], t2["exit_reason"], t2["pnl_ticks"]) == (
        px(5009.25), px(5005.5), "STOP", -15)
    assert t1["initial_risk_ticks"] == 14 and t1["net_sign"] == -1 and t1["r_research"] is None
    assert set(json.loads(t1["flags_json"])) == {"AMBIGUOUS_ENTRY", "FEES_NOT_CONFIGURED", "RESEARCH_RISK_UNIT_NOT_SET"}
    assert t1["gross_usd"] == "-187.50" and t1["fees_usd"] == "0"

    fills_ = rows(env, "SELECT kind, reason, price_ticks FROM paper_fills WHERE run_id=? ORDER BY fill_time_utc, kind DESC", run_id)
    assert [(f["kind"], f["reason"]) for f in fills_] == [("ENTRY", "intrabar"), ("EXIT", "STOP")] * 2
    pos = rows(env, "SELECT status, target_ticks, initial_stop_ticks FROM paper_positions WHERE run_id=?", run_id)
    assert {p["status"] for p in pos} == {"CLOSED"} and px(5017.25) in {p["target_ticks"] for p in pos}   # 2R

    ev = {e["as_of_utc"]: e for e in rows(env, "SELECT * FROM scan_events WHERE run_id=?", run_id)}
    h1, h3 = ev[bar_end(5) + 5], ev[bar_end(11) + 5]
    assert (h1["signal_status"], h1["risk_status"], h1["paper_action"]) == ("SETUP_VALID", "PASS", "QUEUE_ENTRY")
    assert h1["strategy_family"] == "pa_brooks" and h1["run_mode"] == "fixture" and h1["snapshot_id"].startswith("snap_")
    d3 = json.loads(h3["decision_json"])
    assert h3["signal_status"] == "WAIT" and d3["decision_metadata"]["skip_reason"] == "TWO_STOPS_HALT"
    assert d3["decision_metadata"]["label"] == "H3" and d3["decision_metadata"]["structure"]["stops_today"] == 2
    held = json.loads(ev[bar_end(6) + 5]["decision_json"])
    assert held["paper_action"] == "MANAGE_ONLY" and [e["kind"] for e in held["events"]] == ["TRIGGERED", "ENTRY"]
    # 策略状态可以从扫描记录恢复
    st = StrategyState.from_json(h1["strategy_state_json"])
    assert st.data["spike"]["labels"] == 1 and st.data["last_signal"]["label"] == "H1"
    run = RN.get_run(env.con, run_id)
    assert run["risk_domain"] == f"fixture:{run_id}" and run["last_bar_end_utc"] == H.et(D1, 16, 0)
    assert StrategyState.from_json(run["state_json"]).data["halted"] is True


def test_version_tuple_bound_to_orders_and_trades(env):
    load(env, scripted_day())
    out = do_replay(env)
    vt = json.loads(rows(env, "SELECT version_tuple_json v FROM paper_orders WHERE run_id=?", out["run_id"])[0]["v"])
    assert vt == {"strategy_family": "pa_brooks", "strategy_version": "v1.0", "risk_version": RISK.version,
                  "prompt_version": None, "memory_version": None, "engine_version": fills.ENGINE_VERSION,
                  "model_id": None, "release_id": "UNRELEASED", "manifest_hash": None}
    for tbl in ("paper_positions", "paper_trades", "scan_events"):
        got = {r["version_tuple_json"] for r in rows(env, f"SELECT version_tuple_json FROM {tbl} WHERE run_id=?", out["run_id"])}
        assert got == {json.dumps(vt)}, tbl


def test_position_keeps_old_version_when_current_changes(env):
    load(env, scripted_day())
    run_id = RN.create_run(env.con, env.scope, PA, "fixture")
    step = lambda i, cfg: RN.step(env.settings, env.con, run_id, PA, bar_end(i) + 5, risk_cfg=cfg, engine_cfg=ENG)   # noqa: E731
    for i in range(-6, 7):
        step(i, RISK)                                      # 到第 6 根：H1 已成交，持仓中
    import copy, yaml
    raw = yaml.safe_load((H.PROJECT / "config" / "risk.yaml").read_text(encoding="utf-8"))
    raw2 = copy.deepcopy(raw)
    raw2["slippage"]["budget_ticks"] = 3
    new = EC.parse_risk(raw2)
    assert new.version != RISK.version
    for i in range(7, 10):
        step(i, new)                                       # 版本变了：旧仓位止损出场，H2 是新订单
    t = rows(env, "SELECT version_tuple_json v FROM paper_trades WHERE run_id=? ORDER BY exit_time_utc", run_id)
    assert json.loads(t[0]["v"])["risk_version"] == RISK.version               # 旧仓位记旧版本
    o = rows(env, "SELECT version_tuple_json v FROM paper_orders WHERE run_id=? ORDER BY armed_at_utc", run_id)
    assert [json.loads(x["v"])["risk_version"] for x in o] == [RISK.version, new.version]


def test_versions_current_with_and_without_release(env):
    from trading_agent import db
    vt = versions.current(env.scope, PA, RISK)
    assert vt["release_id"] == "UNRELEASED" and set(versions.FIELDS) <= set(vt)
    rel = db.open_release(env.settings)
    assert versions.current(env.scope, PA, RISK, rel)["release_id"] == "UNRELEASED"
    rel.execute("INSERT INTO active_pointers VALUES ('pa_brooks','ES','RTH','5m','rel-1','abc123',1,1,1)")
    got = versions.current(env.scope, PA, RISK, rel)
    assert (got["release_id"], got["manifest_hash"]) == ("rel-1", "abc123")
    rel.rollback()
    rel.close()


# ---------------------------------------------------------------- 6.1 / 6.3 / 6.6 时钟与幂等
def dump(env, run_id):
    out = {}
    for tbl, order in (("paper_orders", "order_id"), ("paper_fills", "fill_id"), ("paper_positions", "position_id"),
                       ("paper_trades", "trade_id"), ("scan_events", "event_id")):
        drop = {"created_at_utc", "updated_at_utc", "run_id", "risk_domain", "decision_json", "order_id", "fill_id",
                "position_id", "trade_id", "event_id"}
        out[tbl] = [{k: v for k, v in r.items() if k not in drop}
                    for r in rows(env, f"SELECT * FROM {tbl} WHERE run_id=? ORDER BY {order}", run_id)]
        key = lambda r: json.dumps(r, sort_keys=True, default=str)      # noqa: E731
        out[tbl].sort(key=key)
    return out


def test_clock_only_moves_forward(env):
    load(env, scripted_day())
    run_id = RN.create_run(env.con, env.scope, PA, "fixture")
    RN.step(env.settings, env.con, run_id, PA, bar_end(3) + 5, risk_cfg=RISK, engine_cfg=ENG)
    before = (dump(env, run_id), dict(RN.get_run(env.con, run_id)))
    with pytest.raises(RN.ClockError, match="早于运行的水位"):
        RN.step(env.settings, env.con, run_id, PA, bar_end(1) + 5, risk_cfg=RISK, engine_cfg=ENG)
    assert (dump(env, run_id), dict(RN.get_run(env.con, run_id))) == before
    assert not env.con.in_transaction


def test_step_is_idempotent(env):
    load(env, scripted_day())
    run_id = RN.create_run(env.con, env.scope, PA, "fixture")
    results = {}
    for i in range(-6, 8):
        results[i] = RN.step(env.settings, env.con, run_id, PA, bar_end(i) + 5, risk_cfg=RISK, engine_cfg=ENG)
    snap = (dump(env, run_id), dict(RN.get_run(env.con, run_id)))
    again = RN.step(env.settings, env.con, run_id, PA, bar_end(7) + 5, risk_cfg=RISK, engine_cfg=ENG)
    assert again["status"] == "ALREADY_PROCESSED"
    assert {k: v for k, v in again.items() if k != "status"} == {k: v for k, v in results[7].items() if k != "status"}
    later = RN.step(env.settings, env.con, run_id, PA, bar_end(7) + 60, risk_cfg=RISK, engine_cfg=ENG)   # 同一根 K，晚一点再跑
    assert later["status"] == "ALREADY_PROCESSED"
    assert (dump(env, run_id), dict(RN.get_run(env.con, run_id))) == snap


def test_two_replays_are_identical(env):
    load(env, scripted_day())
    a, b = do_replay(env), do_replay(env)
    assert a["run_id"] != b["run_id"] and {k: v for k, v in a.items() if k != "run_id"} == {k: v for k, v in b.items() if k != "run_id"}
    assert dump(env, a["run_id"]) == dump(env, b["run_id"])
    da = [json.loads(r["decision_json"]) for r in rows(env, "SELECT decision_json FROM scan_events WHERE run_id=? ORDER BY as_of_utc", a["run_id"])]
    db_ = [json.loads(r["decision_json"]) for r in rows(env, "SELECT decision_json FROM scan_events WHERE run_id=? ORDER BY as_of_utc", b["run_id"])]
    strip = lambda d: json.loads(re.sub(r"(ord|pos|scan|run)_[0-9a-f]+", "ID", json.dumps({k: v for k, v in d.items() if k != "run_id"})))   # noqa: E731
    assert [strip(x) for x in da] == [strip(x) for x in db_] and len(da) == 78


def test_runs_are_isolated(env):
    load(env, scripted_day())
    a, b = do_replay(env), do_replay(env)
    for tbl in ("paper_orders", "paper_trades", "paper_positions", "paper_fills"):
        ra = {r[0] for r in env.con.execute(f"SELECT rowid FROM {tbl} WHERE run_id=?", (a["run_id"],))}
        rb = {r[0] for r in env.con.execute(f"SELECT rowid FROM {tbl} WHERE run_id=?", (b["run_id"],))}
        assert ra and rb and not (ra & rb)
    doms = {r[0] for r in env.con.execute("SELECT DISTINCT risk_domain FROM paper_trades")}
    assert doms == {f"fixture:{a['run_id']}", f"fixture:{b['run_id']}"}
    assert env.con.execute("SELECT COUNT(*) FROM account_risk_days").fetchone()[0] == 0      # 纸面止损不碰账户记录


# ---------------------------------------------------------------- 6.4 订单的时间窗口
def run_until(env, strategy, upto, lag, run_id=None):
    run_id = run_id or RN.create_run(env.con, env.scope, strategy, "fixture")
    res = None
    for i in range(-6, upto + 1):
        res = RN.step(env.settings, env.con, run_id, strategy, bar_end(i) + lag, risk_cfg=RISK, engine_cfg=ENG)
    return run_id, res


def test_slightly_late_activation(env):
    load(env, scripted_day(fetched_lag=1))                 # 每根 K 收盘后 1 秒可见
    run_id, res = run_until(env, PA, 5, lag=2)             # 09:xx:02 处理
    o = rows(env, "SELECT armed_at_utc, expires_at_utc, status FROM paper_orders WHERE run_id=?", run_id)[0]
    assert (o["armed_at_utc"], o["expires_at_utc"], o["status"]) == (bar_end(5) + 2, bar_end(6), "ARMED")
    assert res["paper_action"] == "QUEUE_ENTRY"


def test_signal_too_late_is_not_queued(env):
    load(env, scripted_day(fetched_lag=1))
    run_id, _ = run_until(env, PA, 4, lag=2)
    late = RN.step(env.settings, env.con, run_id, PA, bar_end(5) + 330, risk_cfg=RISK, engine_cfg=ENG)
    # 10:30 收盘的信号直到 10:35:30 才被处理：落下的两根 K 按顺序补算，策略状态保持连续，但旧信号不变成订单
    assert late["signal_bar_end_utc"] == bar_end(6) and late["catch_up"] is False
    (missed,) = late["caught_up"]
    assert (missed["signal_bar_end_utc"], missed["signal_status"], missed["block_reason"]) == (
        bar_end(5), "SETUP_VALID", "MISSED_LATE_SIGNAL")
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    assert env.con.execute("SELECT COUNT(*) FROM scan_events WHERE run_id=?", (run_id,)).fetchone()[0] == 13
    st = StrategyState.from_json(RN.get_run(env.con, run_id)["state_json"]).data
    assert st["spike"]["labels"] == 1                        # H1 的标签已经用掉，和逐根运行时一致


def test_missed_late_signal_recorded(env):
    """信号 K 自己可见得太晚：决策时点已不早于过期时刻，不生成订单，记 MISSED_LATE_SIGNAL。"""
    rows_ = scripted_day(fetched_lag=1)
    sig_start = sessions.iso(bar_end(5) - 300)
    nxt_start = sessions.iso(bar_end(6) - 300)
    for r in rows_:
        if r["bar_start_utc"] == sig_start:
            r["fetched_at_utc"] = sessions.iso(bar_end(6) + 30)         # 10:30 收盘，10:35:30 才取到
        elif r["bar_start_utc"] == nxt_start:
            r["fetched_at_utc"] = sessions.iso(bar_end(6) + 400)        # 下一根更晚才到
    load(env, rows_)
    run_id, _ = run_until(env, PA, 4, lag=2)
    res = RN.step(env.settings, env.con, run_id, PA, bar_end(6) + 31, risk_cfg=RISK, engine_cfg=ENG)
    assert res["signal_bar_end_utc"] == bar_end(5) and res["signal_status"] == "SETUP_VALID"
    assert (res["block_reason"], res["order_id"], res["paper_action"]) == ("MISSED_LATE_SIGNAL", None, "NONE")
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=?", (run_id,)).fetchone()[0] == 0


# ---------------------------------------------------------------- 6.5 冻结
def test_freeze_blocks_new_entries(env):
    load(env, scripted_day())
    run_id, _ = run_until(env, PA, 4, lag=5)
    run = RN.get_run(env.con, run_id)
    RN.set_freeze(env.con, env.scope, run["risk_domain"], frozen=True, cause="MANUAL", reason="test", actor="trader")
    res = RN.step(env.settings, env.con, run_id, PA, bar_end(5) + 5, risk_cfg=RISK, engine_cfg=ENG)
    assert (res["signal_status"], res["risk_status"], res["block_reason"], res["order_id"]) == (
        "SETUP_VALID", "BLOCKED", "FROZEN:MANUAL", None)
    ev = rows(env, "SELECT risk_status, paper_action FROM scan_events WHERE event_id=?", res["event_id"])[0]
    assert (ev["risk_status"], ev["paper_action"]) == ("BLOCKED", "NONE")
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    RN.set_freeze(env.con, env.scope, run["risk_domain"], frozen=False, cause="MANUAL")
    assert RN.frozen_reason(env.con, env.scope, run["risk_domain"]) is None
    RN.set_freeze(env.con, env.scope, "*", frozen=True, cause="DAILY_LOSS")
    RN.set_freeze(env.con, env.scope, "*", frozen=True, cause="MONITOR")
    RN.set_freeze(env.con, env.scope, "*", frozen=False, cause="MONITOR")
    assert RN.frozen_reason(env.con, env.scope, run["risk_domain"]) == "FROZEN:DAILY_LOSS"     # 各原因分别解除


def test_data_block_also_blocks(env):
    load(env, scripted_day())
    run_id, _ = run_until(env, PA, 4, lag=5)
    run = RN.get_run(env.con, run_id)
    env.con.execute("INSERT INTO scope_data_status VALUES (?,?,1,'UNEXPECTED_GAP',1,1,1)", (str(env.scope), run["risk_domain"]))
    env.con.commit()
    res = RN.step(env.settings, env.con, run_id, PA, bar_end(5) + 5, risk_cfg=RISK, engine_cfg=ENG)
    assert res["risk_status"] == "BLOCKED" and res["block_reason"] == "DATA_BLOCKED:UNEXPECTED_GAP"


# ---------------------------------------------------------------- 6.7 可见性
def test_signal_bar_not_yet_visible(env):
    load(env, scripted_day())                              # 可用时点 = 收盘 + 5 秒
    run_id, _ = run_until(env, PA, 4, lag=5)
    early = RN.step(env.settings, env.con, run_id, PA, bar_end(5) + 2, risk_cfg=RISK, engine_cfg=ENG)
    assert early["status"] == "ALREADY_PROCESSED"          # 第 5 根还不可见，最新的仍是第 4 根
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    ok = RN.step(env.settings, env.con, run_id, PA, bar_end(5) + 5, risk_cfg=RISK, engine_cfg=ENG)
    assert ok["signal_status"] == "SETUP_VALID" and ok["order_id"]


def test_later_revision_does_not_rewrite_history(env):
    rows_ = scripted_day(fetched_lag=1)
    load(env, rows_)
    run_id, _ = run_until(env, PA, 7, lag=2)               # H1 成交并止损
    before = dump(env, run_id)
    sig = next(r for r in rows_ if r["bar_start_utc"] == sessions.iso(bar_end(5) - 300))
    revised = dict(sig, close="5007.5", high="5008", fetched_at_utc=sessions.iso(bar_end(7) + 100))   # 事后把信号 K 改成不合格
    H.write_csv(env.root / "fixture_day.csv", [revised], mode="a")
    env.ingest()
    assert env.count("revision=2 AND timeframe='5m' AND derived=0") == 1
    assert dump(env, run_id) == before                     # 已有的信号、订单、成交、交易都不变
    res = RN.step(env.settings, env.con, run_id, PA, bar_end(8) + 200, risk_cfg=RISK, engine_cfg=ENG)
    assert res["status"] == "OK"                           # 之后的决策照常进行，用的是当时可见的数据
    kept = dump(env, run_id)
    assert kept["paper_trades"] == before["paper_trades"]


# ---------------------------------------------------------------- 6.8 模拟策略
def test_mock_strategy_replay(env):
    load(env, H.eth_bars(D1) + H.rth_bars(D1) + H.eth_bars(D2) + H.rth_bars(D2))
    out = do_replay(env, MOCK, D1, D2)
    assert out["steps"] == 156 and out["trades"] >= 2 and out["orders"] >= 2
    t = rows(env, "SELECT * FROM paper_trades WHERE run_id=? ORDER BY exit_time_utc", out["run_id"])
    assert {x["trading_day"] for x in t} == {"2025-11-25", "2025-11-26"} and {x["side"] for x in t} == {"LONG"}
    assert set(x["exit_reason"] for x in t) <= {"STOP", "TARGET", "FORCED_FLAT"}
    o = rows(env, "SELECT order_type, target_json, trigger_ticks FROM paper_orders WHERE run_id=?", out["run_id"])
    assert {x["order_type"] for x in o} == {"MARKET_NEXT_OPEN"} and {x["trigger_ticks"] for x in o} == {None}
    assert {x["target_json"] for x in o} == {'{"kind": "INITIAL_RISK_MULTIPLE", "value": 2}'}
    # 每个交易日收盘时都没有持仓；截止之后没有新订单
    assert env.con.execute("SELECT COUNT(*) FROM paper_positions WHERE run_id=? AND status='OPEN'", (out["run_id"],)).fetchone()[0] == 0
    cut = H.et(D1, 15, 55)
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders WHERE run_id=? AND armed_at_utc>=? AND armed_at_utc<?",
                           (out["run_id"], cut, H.et(D1, 18, 0))).fetchone()[0] == 0
    after = rows(env, "SELECT decision_json d FROM scan_events WHERE run_id=? AND as_of_utc=?", out["run_id"], H.et(D1, 16, 0) + 5)[0]
    assert json.loads(after["d"])["block_reason"] == "AFTER_CUTOFF"
    blocked = [json.loads(r["d"])["block_reason"] for r in rows(env, "SELECT decision_json d FROM scan_events WHERE run_id=?", out["run_id"])]
    assert "POSITION_OPEN_SAME_SIDE" in blocked


def test_mock_forced_flat_through_runner(env):
    """平盘的一天：既到不了目标也碰不到止损，持仓到 15:55 被强平。"""
    flat = (5000, 5001, 4999, 5000)
    load(env, scripted_day(script=[], default=flat))
    out = do_replay(env, MOCK)
    t = rows(env, "SELECT * FROM paper_trades WHERE run_id=?", out["run_id"])
    assert len(t) == 1 and t[0]["exit_reason"] == "FORCED_FLAT" and t[0]["exit_time_utc"] == H.et(D1, 15, 55)
    assert t[0]["exit_ticks"] == px(5000) - 1               # 开盘价减 1 tick 滑点


def test_engine_has_no_strategy_specific_terms():
    src = H.PROJECT / "src" / "trading_agent" / "engine"
    files = sorted(src.glob("*.py"))
    assert len(files) >= 6
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"pa_brooks|PaBrooks|spike|pullback|\bH[123]\b|\bL[123]\b|brooks", text, re.I), f


def test_wrong_strategy_for_run_rejected(env):
    load(env, scripted_day())
    run_id = RN.create_run(env.con, env.scope, PA, "fixture")
    with pytest.raises(ValueError, match="不能用 mock 步进"):
        RN.step(env.settings, env.con, run_id, MOCK, bar_end(0) + 5, risk_cfg=RISK, engine_cfg=ENG)


# ---------------------------------------------------------------- 3.9 一分钟子 K
def test_one_minute_sub_bars_give_clean_entry(env):
    load(env, scripted_day(one_minute=True))
    out = do_replay(env)
    run_id = out["run_id"]
    assert out["trades"] == 2
    o = rows(env, "SELECT flags_json, status FROM paper_orders WHERE run_id=? ORDER BY armed_at_utc", run_id)
    assert [json.loads(x["flags_json"]) for x in o] == [[], []]          # 在第 3 分钟触发：完全在激活之后，没有歧义
    ev = rows(env, "SELECT decision_json d FROM scan_events WHERE run_id=? AND as_of_utc=?", run_id, bar_end(6) + 5)[0]
    assert json.loads(ev["d"])["sub_bar_timeframe"] == "1m"
    f = rows(env, "SELECT kind, fill_time_utc FROM paper_fills WHERE run_id=? ORDER BY fill_time_utc LIMIT 1", run_id)[0]
    assert f["fill_time_utc"] == bar_end(5) + 180                         # 第 3 根 1 分钟线结束时
    t = rows(env, "SELECT flags_json, exit_reason FROM paper_trades WHERE run_id=? ORDER BY exit_time_utc", run_id)
    assert all("AMBIGUOUS_ENTRY" not in json.loads(x["flags_json"]) and "AMBIGUOUS_BAR" not in json.loads(x["flags_json"]) for x in t)


# ---------------------------------------------------------------- 8.4 真实数据的保护
def test_replay_refuses_real_data_without_boundary(env):
    load(env, [dict(r, fixture="") for r in scripted_day()], name="real_day.csv")
    with pytest.raises(RN.ReplayRefused, match="拒绝在真实数据上回放"):
        do_replay(env)
    with pytest.raises(RN.ReplayRefused):
        do_replay(env, allow_real_data=True)                # 允许了，但没有配置开发可见截止日
    assert env.con.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0] == 0


def test_replay_on_real_data_needs_cutoff_and_flag(tmp_path):
    from conftest import Env
    e = Env(tmp_path, dev_visible_through=D1)
    H.write_csv(e.root / "real_day.csv", [dict(r, fixture="") for r in scripted_day()])
    e.ingest()
    with pytest.raises(RN.ReplayRefused):
        RN.replay(e.settings, e.con, e.scope, PA, D1, D2, risk_cfg=RISK, engine_cfg=ENG, allow_real_data=True)   # 超过截止日
    out = RN.replay(e.settings, e.con, e.scope, PA, D1, D1, risk_cfg=RISK, engine_cfg=ENG, allow_real_data=True)
    assert out["run_mode"] == "historical_replay" and out["trades"] == 2
    e.con.close()
