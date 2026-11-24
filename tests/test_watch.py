import dataclasses
import inspect
import json
import re
from decimal import Decimal

import pytest

import helpers as H
import manual_helpers as M
import test_engine_runner as T
import test_risk as TR
from trading_agent.engine import risk as R
from trading_agent.manual import broker as B, discipline as DC, watch as W

NOW = T.bar_end(5) + 10
DAY = "2025-11-25"


def rules(r):
    return [a["rule"] for a in r["alerts"]]


def alerts(env, rule=None):
    q = "SELECT * FROM discipline_alerts" + (" WHERE rule=?" if rule else "") + " ORDER BY created_at_utc"
    return [dict(x) for x in env.con.execute(q, ([rule] if rule else []))]


def quiet(**off):
    """关掉指定规则，便于单独测一条。"""
    return dataclasses.replace(M.MAN, rules={**M.MAN.rules, **{k: False for k in off}})


ONLY = lambda *keep: dataclasses.replace(M.MAN, rules={r: r in keep for r in M.MAN.rules})    # noqa: E731


# ---------------------------------------------------------------- 5.1 适配器
def test_file_adapter_reads_snapshot(env):
    M.snapshot(env, NOW - 5, [M.pos(2, 5000.25, unrealized=-120.5), M.pos(0)], [M.stop_order(4995, qty=2)])
    s = B.FileSnapshotAdapter(env.settings).fetch(M.ACCT)
    assert (s.account_id, s.fetched_at_utc, s.source) == (M.ACCT, NOW - 5, "file_snapshot")
    (p,) = s.positions
    assert (p.contract_id, p.quantity, p.side, p.avg_price_ticks, p.unrealized_usd) == ("ESZ2025", 2, "LONG", 20001, Decimal("-120.5"))
    (o,) = s.open_orders
    assert (o.order_id, o.side, o.kind, o.price_ticks, o.quantity) == ("S1", "SELL", "STOP", 19980, 2)
    assert len(s.digest()) == 64


def test_file_adapter_failures(env):
    ad = B.FileSnapshotAdapter(env.settings)
    with pytest.raises(B.BrokerReadError, match="没有账户快照文件"):
        ad.fetch(M.ACCT)
    p = M.snapshot(env, NOW)
    p.write_text("{not json")
    with pytest.raises(B.BrokerReadError, match="无法解析"):
        ad.fetch(M.ACCT)
    p.write_text(json.dumps({"fetched_at_utc": "2025-11-25T15:30:00Z", "open_orders": [
        {"order_id": "1", "contract": "ESZ5", "side": "BUY", "kind": "ICEBERG", "quantity": 1}]}))
    with pytest.raises(B.BrokerReadError):
        ad.fetch(M.ACCT)


def test_adapter_interface_is_read_only():
    methods = [n for n, _ in inspect.getmembers(B.BrokerReadOnly, inspect.isfunction) if not n.startswith("_")]
    assert methods == ["fetch"]
    public = [n for n, _ in inspect.getmembers(B.FileSnapshotAdapter, inspect.isfunction) if not n.startswith("_")]
    assert sorted(public) == ["fetch", "path"]
    text = (H.PROJECT / "src" / "trading_agent" / "manual" / "broker.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith(("#", '"""')) and "下单" not in l)
    assert not re.search(r"place|submit|replace_order|cancel|modify|write_text|open\([^)]*['\"]w", code)


# ---------------------------------------------------------------- 5.2 采集与过期
def test_poll_saves_snapshot_and_heartbeat(env):
    M.snapshot(env, NOW - 5)
    r = M.poll(env, NOW)
    assert r["status"] == "OK" and r["alerts"] == [] and r["snapshot_id"].startswith("bs_")
    s = dict(env.con.execute("SELECT * FROM broker_snapshots").fetchone())
    assert (s["account_id"], s["fetched_at_utc"], s["source"]) == (M.ACCT, NOW - 5, "file_snapshot") and len(s["content_sha256"]) == 64
    assert W.heartbeat(env.con, M.ACCT) == {"at_utc": NOW, "ok": True, "interval_seconds": 60}


def test_read_failure_only_raises_data_alert(env):
    r = M.poll(env, NOW)
    assert r["status"] == "READ_FAILED" and r["alerts"] == [] and alerts(env) == []
    a = [dict(x) for x in env.con.execute("SELECT level, kind, message FROM alerts")]
    assert [(x["level"], x["kind"]) for x in a] == [("WARN", "BROKER_DATA_STALE")] and "不评估任何规则" in a[0]["message"]
    assert W.heartbeat(env.con, M.ACCT)["ok"] is False


def test_stale_snapshot_is_not_evaluated(env):
    M.snapshot(env, NOW - 121, [M.pos(1)])                 # 裸仓，但数据过期
    r = M.poll(env, NOW)
    assert r["status"] == "STALE" and alerts(env) == [] and "数据时点" in r["message"]
    assert W.stale_after(60) == 120 and W.stale_after(30) == 120 and W.stale_after(300) == 600


def test_stale_threshold_scales_with_interval(env):
    M.snapshot(env, NOW - 400, [])
    assert M.poll(env, NOW, interval=300)["status"] == "OK"
    assert M.poll(env, NOW + 1, interval=60)["status"] == "STALE"


# ---------------------------------------------------------------- 5.3 裸仓与止损被移远
def test_naked_position_after_grace(env):
    man = ONLY("NAKED_POSITION")
    M.snapshot(env, NOW, [M.pos(1)])
    assert rules(M.poll(env, NOW + 1, man=man)) == []       # 第一次看到：开始计时
    M.snapshot(env, NOW + 61, [M.pos(1)])
    r = M.poll(env, NOW + 62, man=man)
    assert rules(r) == ["NAKED_POSITION"]
    a = r["alerts"][0]
    assert a["level"] == "CRITICAL" and "没有对应的保护止损单" in a["message"] and "ESZ2025 多仓 1 手" in a["message"]
    row = alerts(env)[0]
    assert (row["broker_snapshot_id"], row["position_key"]) == (r["snapshot_id"], f"{M.ACCT}:ESZ2025:LONG")
    assert not re.search(r"应该|建议|请立即|买入|卖出|平仓", a["message"])      # 只陈述事实和规则


def test_naked_resolved_within_grace(env):
    man = ONLY("NAKED_POSITION")
    M.snapshot(env, NOW, [M.pos(1)])
    M.poll(env, NOW + 1, man=man)
    M.snapshot(env, NOW + 30, [M.pos(1)], [M.stop_order(4995)])
    assert rules(M.poll(env, NOW + 31, man=man)) == []
    M.snapshot(env, NOW + 200, [M.pos(1)], [M.stop_order(4995)])
    assert rules(M.poll(env, NOW + 201, man=man)) == [] and alerts(env) == []


def test_grace_follows_interval(env):
    man = ONLY("NAKED_POSITION")
    M.snapshot(env, NOW, [M.pos(1)])
    M.poll(env, NOW + 1, interval=300, man=man)
    M.snapshot(env, NOW + 120, [M.pos(1)])
    assert rules(M.poll(env, NOW + 121, interval=300, man=man)) == []          # 5 分钟模式：120 秒还在宽限内
    M.snapshot(env, NOW + 301, [M.pos(1)])
    assert rules(M.poll(env, NOW + 302, interval=300, man=man)) == ["NAKED_POSITION"]


def test_insufficient_or_wrong_stop_is_naked(env):
    man = ONLY("NAKED_POSITION")
    for orders in ([M.stop_order(4995, qty=1)],                       # 数量不够（持仓 2 手）
                   [M.stop_order(4995, side="BUY", qty=2)],           # 方向不对
                   [M.stop_order(5010, kind="LIMIT", qty=2)]):        # 不是止损类型
        M.snapshot(env, NOW, [M.pos(2)], orders)
        assert W._protective_stop(B.FileSnapshotAdapter(env.settings).fetch(M.ACCT), B.PositionView("ESZ2025", 2)) is None
    M.snapshot(env, NOW, [M.pos(-2)], [M.stop_order(5005, side="BUY", qty=2)])
    assert W._protective_stop(B.FileSnapshotAdapter(env.settings).fetch(M.ACCT), B.PositionView("ESZ2025", -2)) is not None


def test_stop_moved_away(env):
    man = ONLY("STOP_MOVED_AWAY")
    M.snapshot(env, NOW, [M.pos(1)], [M.stop_order(4995)])
    assert rules(M.poll(env, NOW + 1, man=man)) == []
    M.snapshot(env, NOW + 60, [M.pos(1)], [M.stop_order(4998)])       # 上移：不算
    assert rules(M.poll(env, NOW + 61, man=man)) == []
    M.snapshot(env, NOW + 120, [M.pos(1)], [M.stop_order(4990)])      # 下移
    r = M.poll(env, NOW + 121, man=man)
    assert rules(r) == ["STOP_MOVED_AWAY"] and "从 4998.00 被改到 4990.00" in r["alerts"][0]["message"]
    assert r["alerts"][0]["level"] == "WARN"


def test_stop_removed_while_position_remains(env):
    man = ONLY("STOP_MOVED_AWAY")
    M.snapshot(env, NOW, [M.pos(-1)], [M.stop_order(5005, side="BUY")])
    M.poll(env, NOW + 1, man=man)
    M.snapshot(env, NOW + 60, [M.pos(-1)], [])
    r = M.poll(env, NOW + 61, man=man)
    assert rules(r) == ["STOP_MOVED_AWAY"] and "已不在挂单中" in r["alerts"][0]["message"]


# ---------------------------------------------------------------- 5.4 无计划开仓与冷却期
def test_unplanned_entry(env):
    man = ONLY("UNPLANNED_ENTRY")
    M.snapshot(env, NOW, [M.pos(1)], [M.stop_order(4995)])
    r = M.poll(env, NOW + 1, man=man)
    assert rules(r) == ["UNPLANNED_ENTRY"] and "没有找到相符的已激活计划" in r["alerts"][0]["message"]


def test_planned_entry_is_quiet(env):
    man = ONLY("UNPLANNED_ENTRY")
    pid = M.mkplan(env, "ES L 5000 4995 5010 1 plan", now=NOW - 100)["plan_id"]
    M.activate(env, pid, now=NOW - 90)
    M.snapshot(env, NOW, [M.pos(1)], [M.stop_order(4995)])
    assert rules(M.poll(env, NOW + 1, man=man)) == []
    M.snapshot(env, NOW + 60, [M.pos(1), M.pos(-1, contract="NQZ5")])          # 另一个品种没有计划
    assert [a["position_key"] for a in M.poll(env, NOW + 61, man=man)["alerts"]] == [f"{M.ACCT}:NQZ2025:SHORT"]


def stop_out(env, at):
    M.do_import(env, [M.trade(at - 300, "BUY", 5000, fill="o", order="E1"), M.trade(at, "SELL", 4995, fill="c", order="S1")],
                [M.oevent(at, "S1", "FILLED", "STOP", 4995)])


def test_reentry_in_cooldown(env):
    man = ONLY("REENTRY_IN_COOLDOWN")
    stop_out(env, NOW - 300)                                # 5 分钟前止损
    M.snapshot(env, NOW, [M.pos(1)], [M.stop_order(4995)])
    r = M.poll(env, NOW + 1, man=man)
    assert rules(r) == ["REENTRY_IN_COOLDOWN"]
    msg = r["alerts"][0]["message"]
    assert "冷却 15 分钟" in msg and "已激活计划：无" in msg and "账户停手：否" in msg and "新的有效 setup：未知" in msg
    d = json.loads(alerts(env)[0]["detail_json"])
    assert d["account_halted"] is False and d["active_plan"] is None and d["new_setup"] is None


def test_no_reentry_alert_after_cooldown_or_other_side(env):
    man = ONLY("REENTRY_IN_COOLDOWN")
    stop_out(env, NOW - 16 * 60)
    M.snapshot(env, NOW, [M.pos(1)])
    assert rules(M.poll(env, NOW + 1, man=man)) == []
    stop_out_at = NOW + 100
    M.do_import(env, [M.trade(stop_out_at, "SELL", 4995, fill="c2", order="S2")], [M.oevent(stop_out_at, "S2", "FILLED", "STOP", 4995)], name="x")
    M.snapshot(env, NOW + 200, [M.pos(-1)])                 # 反方向
    assert rules(M.poll(env, NOW + 201, man=man)) == []


# ---------------------------------------------------------------- 5.5 逆势加仓与日损
def test_countertrend_add(env):
    man = ONLY("COUNTERTREND_ADD")
    now = M.paper_state(env, 4, mirror=True)                # 熊 Spike 的回调中
    M.snapshot(env, now, [M.pos(1, 4995)])
    assert rules(M.poll(env, now + 1, man=man)) == []       # 新开仓不是"加仓"
    M.snapshot(env, now + 60, [M.pos(2, 4994)])
    r = M.poll(env, now + 61, man=man)
    assert rules(r) == ["COUNTERTREND_ADD"] and "由 1 手增加" in r["alerts"][0]["message"]
    M.snapshot(env, now + 120, [M.pos(-1, 4994, contract="NQZ5")])
    assert rules(M.poll(env, now + 121, man=man)) == []


def test_countertrend_add_unknown_without_strategy_state(env):
    man = ONLY("COUNTERTREND_ADD")
    M.snapshot(env, NOW, [M.pos(1)])
    M.poll(env, NOW + 1, man=man)
    M.snapshot(env, NOW + 60, [M.pos(2)])
    r = M.poll(env, NOW + 61, man=man)
    assert rules(r) == [] and r["unknown"] == [{"rule": "COUNTERTREND_ADD", "position_key": f"{M.ACCT}:ESZ2025:LONG",
                                                "why": "没有可用的策略状态"}]


def test_daily_loss_near_limit(env):
    man = ONLY("DAILY_LOSS_NEAR_LIMIT")
    with env.con:
        R.record_account_exit(env.con, M.FUNDED, account_id=M.ACCT, trading_day=DAY, source_id="x", exit_reason="OTHER",
                              net_usd=Decimal(-500))
    M.snapshot(env, NOW, [M.pos(1, unrealized=-299)])
    assert rules(M.poll(env, NOW + 1, man=man)) == []       # 799 < 800
    M.snapshot(env, NOW + 60, [M.pos(1, unrealized=-320)])
    r = M.poll(env, NOW + 61, man=man)
    assert rules(r) == ["DAILY_LOSS_NEAR_LIMIT"] and "820" in r["alerts"][0]["message"] and "80%" in r["alerts"][0]["message"]


def test_daily_loss_not_evaluated_when_unconfigured(env):
    man = ONLY("DAILY_LOSS_NEAR_LIMIT")
    M.snapshot(env, NOW, [M.pos(1, unrealized=-5000)])
    r = M.poll(env, NOW + 1, man=man, risk=M.UNFUNDED)
    assert rules(r) == [] and r["unknown"] == [{"rule": "DAILY_LOSS_NEAR_LIMIT", "why": "日损上限未配置"}]
    M.snapshot(env, NOW + 60, [M.pos(1)])                   # 快照没有未实现盈亏
    r = M.poll(env, NOW + 61, man=man)
    assert r["unknown"] == [{"rule": "DAILY_LOSS_NEAR_LIMIT", "why": "快照没有未实现盈亏"}]


# ---------------------------------------------------------------- 5.6 去重与解除
def test_quiet_window_and_resolution(env):
    man = ONLY("NAKED_POSITION")
    for i in range(5):                                      # 持续裸仓：每分钟采集一次
        M.snapshot(env, NOW + 60 * i, [M.pos(1)])
        M.poll(env, NOW + 60 * i + 1, man=man)
    assert len(alerts(env, "NAKED_POSITION")) == 1 and alerts(env)[0]["resolved_at_utc"] is None
    M.snapshot(env, NOW + 300, [M.pos(1)], [M.stop_order(4995)])
    M.poll(env, NOW + 301, man=man)
    a = alerts(env)[0]
    assert a["resolved_at_utc"] == NOW + 301
    k = DC.session_kpis(env.con, M.MAN, M.ACCT, DAY)["alerts"]["NAKED_POSITION"]
    assert k == {"count": 1, "resolved": 1, "median_seconds_to_resolve": a["resolved_at_utc"] - a["created_at_utc"],
                 "unresolved_share": 0.0}


def test_alert_again_after_quiet_window(env):
    man = ONLY("NAKED_POSITION")
    for t in (0, 61, 61 + 16 * 60):
        M.snapshot(env, NOW + t, [M.pos(1)])
        M.poll(env, NOW + t + 1, man=man)
    assert len(alerts(env, "NAKED_POSITION")) == 2
    k = DC.session_kpis(env.con, M.MAN, M.ACCT, DAY)["alerts"]["NAKED_POSITION"]
    assert (k["count"], k["resolved"], k["unresolved_share"]) == (2, 0, 1.0)


def test_alerts_resolved_when_position_closes(env):
    M.snapshot(env, NOW, [M.pos(1)])
    M.poll(env, NOW + 1)
    M.snapshot(env, NOW + 61, [M.pos(1)])
    M.poll(env, NOW + 62)
    assert {a["rule"] for a in alerts(env)} == {"UNPLANNED_ENTRY", "NAKED_POSITION"}
    M.snapshot(env, NOW + 120, [])
    M.poll(env, NOW + 121)
    assert all(a["resolved_at_utc"] == NOW + 121 for a in alerts(env))
    kinds = [r[0] for r in env.con.execute("SELECT kind FROM alerts ORDER BY alert_id")]
    assert kinds == ["UNPLANNED_ENTRY", "NAKED_POSITION"]      # 同时进了通用告警表


def test_disabled_rule_does_not_fire(env):
    M.snapshot(env, NOW, [M.pos(1)])
    assert rules(M.poll(env, NOW + 1, man=quiet(UNPLANNED_ENTRY=True))) == []


# ---------------------------------------------------------------- 5.7 间隔与循环
def test_interval_resolution(env):
    assert W.resolve_interval(env.settings.watch) == 60
    assert W.resolve_interval(env.settings.watch, 300) == 300 and W.resolve_interval(env.settings.watch, 17) == 17
    for bad in (0, -5, True, 1.5):
        with pytest.raises(ValueError):
            W.resolve_interval(env.settings.watch, bad)


def test_run_loop():
    calls, sleeps, out = [], [], []
    n = W.run_loop(lambda: calls.append(1) or {"n": len(calls)}, 300, iterations=3, sleep=sleeps.append, emit=out.append)
    assert n == 3 and sleeps == [300, 300] and out == [{"n": 1}, {"n": 2}, {"n": 3}]
