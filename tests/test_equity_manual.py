"""股票类品种（SPY、QQQ）走人工轨：计划、账户限额、Alpaca 只读适配器、成交导入、watch。"""
import json
import re
from datetime import date
from decimal import Decimal

import pytest
import yaml

import helpers as H
import manual_helpers as MH
from trading_agent import contracts, sessions
from trading_agent.cli import main
from trading_agent.config import ConfigError
from trading_agent.engine import config as EC
from trading_agent.manual import alpaca as AL
from trading_agent.manual import common, config as MC, plans, watch as W
from trading_agent.manual.broker import BrokerReadError, FileSnapshotAdapter
from trading_agent.sources import alpaca_rest
from trading_agent.strategies.registry import default_registry

D = date(2025, 11, 25)
NOW = H.et(D, 10, 30)
ACCT = "alpaca-paper"
PA = default_registry().get("pa_brooks")
RISK = MC.risk_for_account(H.PROJECT, ACCT, base=EC.load_risk(H.TEST_PROJECT))
MAN = MC.load_manual(H.TEST_PROJECT)


def iso(hh, mm, ss=0):
    return sessions.iso(H.et(D, hh, mm, ss)).replace("Z", ".123456Z")


POSITIONS = [{"symbol": "SPY", "qty": "100", "side": "long", "avg_entry_price": "700.1234", "unrealized_pl": "-12.5"},
             {"symbol": "QQQ", "qty": "50", "side": "short", "avg_entry_price": "600.5", "unrealized_pl": "3"},
             {"symbol": "AAPL", "qty": "10", "side": "long", "avg_entry_price": "200", "unrealized_pl": "0"},
             {"symbol": "SPY", "qty": "0.5", "side": "long", "avg_entry_price": "700", "unrealized_pl": "0"}]
ORDERS_OPEN = [{"id": "o-stop", "symbol": "SPY", "side": "sell", "type": "stop", "qty": "100", "stop_price": "698.50",
                "limit_price": None, "submitted_at": iso(10, 1)},
               {"id": "o-lim", "symbol": "QQQ", "side": "buy", "type": "limit", "qty": "50", "limit_price": "598",
                "stop_price": None, "submitted_at": iso(10, 2)},
               {"id": "o-x", "symbol": "TSLA", "side": "buy", "type": "market", "qty": "1", "submitted_at": iso(10, 3)}]
ORDERS_ALL = ORDERS_OPEN + [
    {"id": "o-buy", "symbol": "SPY", "side": "buy", "type": "limit", "qty": "100", "limit_price": "700.10",
     "stop_price": None, "submitted_at": iso(9, 59), "filled_at": iso(10, 0)},
    {"id": "o-old", "symbol": "SPY", "side": "sell", "type": "stop", "qty": "100", "stop_price": "699",
     "limit_price": None, "submitted_at": iso(10, 0, 30), "canceled_at": iso(10, 1)}]
FILLS = [{"id": "f1", "activity_type": "FILL", "transaction_time": iso(10, 0), "type": "fill", "price": "700.1049",
          "qty": "100", "side": "buy", "symbol": "SPY", "order_id": "o-buy"},
         {"id": "f9", "activity_type": "FILL", "transaction_time": iso(10, 5), "type": "fill", "price": "10",
          "qty": "3", "side": "buy", "symbol": "AAPL", "order_id": "zz"}]


class FakeTransport:
    def __init__(self, at=NOW, **data):
        self.data = {"positions": POSITIONS, "orders_open": ORDERS_OPEN, "orders_all": ORDERS_ALL, "fills": FILLS, **data}
        self.at, self.calls = at, []

    def get(self, name):
        self.calls.append(name)
        return self.data[name], self.at


# ---------------------------------------------------------------- 品种登记
def test_equity_instruments_registered_without_touching_futures(env):
    st = env.settings
    spy, es = st.instrument("SPY"), st.instrument("ES")
    assert common.is_equity(st, spy) and common.is_equity(st, st.instrument("QQQ")) and not common.is_equity(st, es)
    assert spy.tick_size == Decimal("0.01") and spy.tick_value_usd == Decimal("0.01") and spy.months == ()
    assert common.active_contract(st, spy, D) == "SPY" and common.active_contract(st, es, D) == "ESZ2025"
    assert common.resolve_contract(st, "spy", D)[0] == "SPY" and common.resolve_contract(st, "ESZ5", D)[0] == "ESZ2025"
    assert common.instrument_of(st, "QQQ").root == "QQQ" and common.instrument_of(st, "ESZ2025").root == "ES"
    assert common.unit(st, spy) == "股" and common.unit(st, es) == "手"
    assert contracts.active_contract(es, D, st.roll_rules) == "ESZ2025"            # 期货的换月不受影响
    with pytest.raises(Exception):
        common.resolve_contract(st, "NOPE", D)


# ---------------------------------------------------------------- 按账户的限额
def test_account_limits_are_per_account_and_do_not_change_paper_risk():
    base = EC.load_risk(H.TEST_PROJECT)
    assert not base.account.approved                                    # 基础配置（期货）仍未批准
    r = MC.risk_for_account(H.PROJECT, ACCT, base=base)
    a = r.account
    assert a.approved and (a.budget_per_trade_usd, a.daily_loss_limit_usd, a.max_contracts) == (250, 1000, 200)
    assert r.round_trip_fee_usd == 0 and r.slippage_budget_ticks == 2 and r.version == f"{base.version}+acct:{ACCT}"
    assert (r.stop_slippage_ticks, r.paper_quantity, r.research_risk_unit_usd) == (
        base.stop_slippage_ticks, base.paper_quantity, base.research_risk_unit_usd)
    assert MC.risk_for_account(H.PROJECT, "someone-else", base=base) is base and MC.risk_for_account(H.PROJECT, None, base=base) is base
    raw = yaml.safe_load((H.PROJECT / "config" / "risk.yaml").read_text(encoding="utf-8"))
    assert raw["account"]["approved"] is False                          # 账户限额不写进 risk.yaml：不改 risk_version


def test_accounts_config_validation(tmp_path):
    (tmp_path / "config").mkdir()
    f = tmp_path / "config" / "accounts.yaml"
    assert MC.load_accounts(tmp_path) == {}
    for bad in ({"a": {"broker": "robinhood"}}, {"a": {"approved": "yes"}}, {"a": 3}):
        f.write_text(yaml.safe_dump({"accounts": bad}), encoding="utf-8")
        with pytest.raises(ConfigError):
            MC.load_accounts(tmp_path)
    f.write_text(yaml.safe_dump({"accounts": {"a": {"budget_per_trade_usd": -1}}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        MC.risk_for_account(tmp_path, "a", base=EC.load_risk(H.TEST_PROJECT))


# ---------------------------------------------------------------- 计划
def snap(env, at=NOW):
    W.poll_once(env.settings, env.con, AL.AlpacaReadOnly(env.settings, FakeTransport(at, positions=[], orders_open=[])),
                ACCT, scope=env.scope, strategy=PA, risk_cfg=RISK, manual_cfg=MAN, interval_seconds=60, now_utc=at)


def make(env, tokens, now=NOW):
    inp = plans.parse_quick(tokens.split(), env.settings, D)
    return plans.create(env.settings, env.con, account_id=ACCT, scope=env.scope, strategy=PA, inp=inp, risk_cfg=RISK,
                        manual_cfg=MAN, now_utc=now, snapshot_fetched_at=now)


def test_equity_plan_precheck_uses_share_risk(env):
    r = make(env, "SPY L 700.50 699.00 705 100 回调买入")
    c = r["check"]
    assert r["contract_id"] == "SPY" and c["risk_status"] == "PASS" and c["strategy_alignment"] == "UNKNOWN"
    assert c["conclusion"] == "DEVIATES" and "没有正式策略" in c["summary"] and "100 股" in c["summary"]
    assert Decimal(r["risk"]["risk_per_contract_usd"]) == Decimal("1.52")       # 1.50 止损距离 + 2 tick 滑点预算，零佣金
    assert "该品种没有正式策略，只评价计划纪律和账户风险" in c["observable_conditions"]
    over = make(env, "SPY L 700.50 699.00 705 200 太大")                   # 200 股 × 1.52 = 304 > 250
    assert over["check"]["conclusion"] == "BLOCKED_BY_RISK" and over["check"]["risk_reason"] == "RISK_OVER_BUDGET"
    assert over["risk"]["quantity_allowed"] == 164
    cap = make(env, "QQQ S 600 600.50 598 300 超过最大股数")                 # 预算够 480 股，最大股数 200
    assert cap["check"]["risk_reason"] == "RISK_OVER_BUDGET" and cap["risk"]["quantity_allowed"] == 200
    nostop = make(env, "SPY L 700 - 705 10 没写止损")
    assert nostop["check"]["risk_reason"] == "NO_STOP" and "NO_STOP" in nostop["check"]["discipline_flags"]
    with pytest.raises(plans.PlanError, match="不合法"):
        plans.parse_quick("SPY L 700.505 699 705 10".split(), env.settings, D)   # 不到一分钱的价格


def test_equity_plan_activation_reserves_budget(env):
    r = make(env, "SPY L 700.50 699.00 705 100 x")
    a = plans.activate(env.settings, env.con, r["plan_id"], scope=env.scope, strategy=PA, risk_cfg=RISK, manual_cfg=MAN,
                       now_utc=NOW + 10, snapshot_fetched_at=NOW + 10)
    assert a["status"] == "ACTIVE"
    row = env.con.execute("SELECT amount_usd, quantity FROM risk_reservations WHERE plan_id=?", (r["plan_id"],)).fetchone()
    assert Decimal(row[0]) == Decimal("152.00") and row[1] == 100


# ---------------------------------------------------------------- Alpaca 适配器
def test_alpaca_snapshot_parsing(env):
    ad = AL.AlpacaReadOnly(env.settings, FakeTransport())
    s = ad.fetch(ACCT)
    assert [(p.contract_id, p.quantity, p.avg_price_ticks) for p in s.positions] == [("SPY", 100, 70012), ("QQQ", -50, 60050)]
    assert s.positions[0].unrealized_usd == Decimal("-12.5") and s.positions[1].side == "SHORT"
    assert [(o.order_id, o.contract_id, o.side, o.kind, o.quantity, o.price_ticks) for o in s.open_orders] == [
        ("o-stop", "SPY", "SELL", "STOP", 100, 69850), ("o-lim", "QQQ", "BUY", "LIMIT", 50, 59800)]
    assert s.source == "alpaca" and s.fetched_at_utc == NOW and ad.ignored == {"AAPL": 1, "TSLA": 1}
    assert ad._t.calls == ["positions", "orders_open"]
    with pytest.raises(BrokerReadError, match="无法解析"):
        AL.AlpacaReadOnly(env.settings, FakeTransport(positions=[{"symbol": "SPY"}])).fetch(ACCT)

    class Boom:
        def get(self, name):
            raise alpaca_rest.AlpacaError("Alpaca 读取失败（positions）：HTTPError")
    with pytest.raises(BrokerReadError, match="HTTPError"):
        AL.AlpacaReadOnly(env.settings, Boom()).fetch(ACCT)


def test_alpaca_dump_transport_and_connector_wrapping(env):
    d = AL.dump_dir(env.settings, ACCT)
    d.mkdir(parents=True)
    wrap = lambda x: {"_alpaca_mcp_security": {"trust": "untrusted_tool_output"}, "data": {"result": x}}   # noqa: E731
    (d / "positions.json").write_text(json.dumps(wrap(POSITIONS[:1])))
    (d / "orders_open.json").write_text(json.dumps({"data": ORDERS_OPEN[:1]}))
    s = AL.adapter_for(env.settings, ACCT, env={}).fetch(ACCT)             # 没有密钥：读保存下来的文件
    assert [(p.contract_id, p.quantity) for p in s.positions] == [("SPY", 100)] and s.open_orders[0].kind == "STOP"
    (d / "positions.json").write_text("{}")
    with pytest.raises(BrokerReadError, match="不是列表"):
        AL.adapter_for(env.settings, ACCT, env={}).fetch(ACCT)
    (d / "positions.json").unlink()
    with pytest.raises(BrokerReadError, match="没有文件"):
        AL.adapter_for(env.settings, ACCT, env={}).fetch(ACCT)


def test_alpaca_sync_imports_fills_and_links_plan(env):
    snap(env, H.et(D, 9, 58))
    r = make(env, "SPY L 700.10 698.50 705 100 计划内", now=H.et(D, 9, 58))
    plans.activate(env.settings, env.con, r["plan_id"], scope=env.scope, strategy=PA, risk_cfg=RISK, manual_cfg=MAN,
                   now_utc=H.et(D, 9, 59), snapshot_fetched_at=H.et(D, 9, 59))
    ad = AL.AlpacaReadOnly(env.settings, FakeTransport())
    out = AL.sync(env.settings, env.con, ad, ACCT, risk_cfg=RISK, manual_cfg=MAN)
    ev, tr = out["results"]
    assert tr["imported"] == 1 and tr["bad_rows"] == 0 and ev["imported"] == 6 and out["ignored_symbols"] == {"AAPL": 1, "TSLA": 1}
    t = env.con.execute("SELECT * FROM manual_trades").fetchone()
    assert (t["contract_id"], t["side"], t["quantity"], t["price_ticks"], t["effect"], t["source"]) == (
        "SPY", "LONG", 100, 70010, "OPEN", "alpaca")                      # 700.1049 按最近的一分钱取整
    # 订单没带计划编号：只有时间吻合，记为待核对，并给出建议的计划
    assert t["plan_link_status"] == "UNKNOWN" and t["suggested_plan_id"] == r["plan_id"] and t["broker_fill_id"] == "f1"
    kinds = dict(env.con.execute("SELECT broker_order_id, order_kind FROM manual_order_events GROUP BY 1"))
    assert kinds["o-stop"] == "STOP" and kinds["o-buy"] == "LIMIT"
    again = AL.sync(env.settings, env.con, ad, ACCT, risk_cfg=RISK, manual_cfg=MAN)      # 幂等
    assert again["results"][1]["imported"] == 0 and again["results"][1]["skipped"] == 1
    assert env.con.execute("SELECT COUNT(*) FROM manual_trades").fetchone()[0] == 1


def test_watch_on_equity_positions_skips_strategy_rules(env):
    ad = AL.AlpacaReadOnly(env.settings, FakeTransport(orders_open=ORDERS_OPEN[1:]))     # SPY 多仓没有止损单
    kw = dict(scope=env.scope, strategy=PA, risk_cfg=RISK, manual_cfg=MAN, interval_seconds=60)
    first = W.poll_once(env.settings, env.con, ad, ACCT, now_utc=NOW, **kw)
    rules = {a["rule"] for a in first["alerts"]}
    assert first["status"] == "OK" and "UNPLANNED_ENTRY" in rules and "COUNTERTREND_ADD" not in rules
    assert any("SPY 多仓 100 股" in a["message"] for a in first["alerts"])
    ad2 = AL.AlpacaReadOnly(env.settings, FakeTransport(NOW + 600, orders_open=ORDERS_OPEN[1:]))
    later = W.poll_once(env.settings, env.con, ad2, ACCT, now_utc=NOW + 600, **kw)
    assert "NAKED_POSITION" in {a["rule"] for a in later["alerts"]}                      # 过了宽限时间仍没有止损单
    ok = AL.AlpacaReadOnly(env.settings, FakeTransport(NOW + 660))                       # 有了止损单
    W.poll_once(env.settings, env.con, ok, ACCT, now_utc=NOW + 660, **kw)
    open_naked = env.con.execute("SELECT COUNT(*) FROM discipline_alerts WHERE rule='NAKED_POSITION'"
                                 " AND position_key LIKE '%SPY%' AND resolved_at_utc IS NULL").fetchone()[0]
    assert open_naked == 0


# ---------------------------------------------------------------- 命令行
def test_cli_plan_and_ingest_alpaca(env, capsys, monkeypatch):
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    code = main(["plan", "SPY", "L", "700.50", "699.00", "705", "100", "测试", "--account", ACCT, "--json"],
                settings=env.settings)
    r = json.loads(capsys.readouterr().out)
    assert code == 0 and r["contract_id"] == "SPY" and r["check"]["risk_reason"] == "BUDGET_NOT_APPROVED"
    # ↑ 测试用的中性配置里没有 accounts.yaml 的批准；真实配置的批准见 test_account_limits_are_per_account…
    code = main(["plan", "--list", "--account", ACCT], settings=env.settings)
    text = capsys.readouterr().out
    assert code == 0 and "入场 700.50" in text and "止损 699.00" in text           # 按 SPY 自己的 tick 显示
    code = main(["ingest", "--alpaca", "--account", ACCT, "--json"], settings=env.settings,
                adapter=AL.AlpacaReadOnly(env.settings, FakeTransport()))
    r = json.loads(capsys.readouterr().out)
    assert code == 0 and r["results"][1]["imported"] == 1
    code = main(["ingest", "--alpaca", "--account", ACCT, "--json"], settings=env.settings)      # 没有密钥也没有文件
    assert code == 1 and json.loads(capsys.readouterr().out)["error"] == "BROKER_READ"


# ---------------------------------------------------------------- 只读保证
def test_alpaca_code_is_read_only():
    src = H.PROJECT / "src" / "trading_agent"
    rest = (src / "sources" / "alpaca_rest.py").read_text(encoding="utf-8")
    manual = (src / "manual" / "alpaca.py").read_text(encoding="utf-8")
    assert re.findall(r'method="(\w+)"', rest) == ["GET"]                   # 只有一处请求，方法是 GET
    assert alpaca_rest.PAPER_HOST == "paper-api.alpaca.markets" and "api.alpaca.markets/" not in rest.replace("paper-api", "")
    for path, _ in alpaca_rest.ENDPOINTS.values():
        assert path in ("/v2/positions", "/v2/orders", "/v2/account/activities/FILL")
    banned = re.compile(r"POST|DELETE|PATCH|PUT\b|data=|place_|submit_order|cancel_order|replace_order|close_position", re.I)
    assert not banned.search(rest) and not banned.search(manual.replace("orders_open", "").replace("orders_all", ""))
    assert not re.search(r"urllib|requests|http\.client|socket", manual)   # 人工轨模块本身没有网络代码
    assert alpaca_rest.credentials(H.TEST_PROJECT, env={}) is None
    assert alpaca_rest.credentials(H.TEST_PROJECT, env={"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s"}) == ("k", "s")


def test_alpaca_fill_links_plan_via_client_order_id(env):
    snap(env, H.et(D, 9, 58))
    r = make(env, "SPY L 700.10 698.50 705 100 计划内", now=H.et(D, 9, 58))
    plans.activate(env.settings, env.con, r["plan_id"], scope=env.scope, strategy=PA, risk_cfg=RISK, manual_cfg=MAN,
                   now_utc=H.et(D, 9, 59), snapshot_fetched_at=H.et(D, 9, 59))
    orders = [dict(o, client_order_id=f"my-{r['plan_id']}-a") if o["id"] == "o-buy" else o for o in ORDERS_ALL]
    ad = AL.AlpacaReadOnly(env.settings, FakeTransport(orders_all=orders))
    AL.sync(env.settings, env.con, ad, ACCT, risk_cfg=RISK, manual_cfg=MAN)
    t = env.con.execute("SELECT plan_link_status, plan_id FROM manual_trades").fetchone()
    assert tuple(t) == ("LINKED", r["plan_id"])


def test_alpaca_replace_chain_becomes_modified_events_and_order_fill_fallback(env):
    """改止损 = 替换成新订单：整条链记在最后那张的订单号下。没有逐笔成交记录时用已成交的订单。"""
    chain = [
        {"id": "e1", "symbol": "QQQ", "side": "sell", "type": "market", "qty": "5", "submitted_at": iso(10, 0),
         "filled_at": iso(10, 0, 5), "filled_qty": "5", "filled_avg_price": "652.6449", "client_order_id": "x"},
        {"id": "s1", "symbol": "QQQ", "side": "buy", "type": "stop", "qty": "5", "stop_price": "654.58",
         "submitted_at": iso(10, 0, 6), "replaced_by": "s2", "replaces": None},
        {"id": "s2", "symbol": "QQQ", "side": "buy", "type": "stop", "qty": "5", "stop_price": "655.10",
         "submitted_at": iso(10, 10), "replaced_by": "s3", "replaces": "s1"},           # 空仓的止损被上移：放宽
        {"id": "s3", "symbol": "QQQ", "side": "buy", "type": "stop", "qty": "5", "stop_price": "650.79",
         "submitted_at": iso(10, 20), "replaces": "s2", "filled_at": iso(10, 25), "filled_qty": "5",
         "filled_avg_price": "650.90"}]

    class NoFills(FakeTransport):
        def get(self, name):
            if name == "fills":
                raise BrokerReadError("没有文件：fills.json")
            return super().get(name)
    ad = AL.AlpacaReadOnly(env.settings, NoFills(orders_all=chain))
    ev = ad.order_event_rows(ACCT)
    stop = [(e["event_type"], e["price"]) for e in ev if e["broker_order_id"] == "s3"]
    assert stop == [("SUBMITTED", "654.58"), ("MODIFIED", "655.10"), ("MODIFIED", "650.79"), ("FILLED", "650.79")]
    tr = ad.trade_rows(ACCT)
    assert [(t["side"], t["quantity"], t["price"], t["broker_order_id"], t["broker_fill_id"]) for t in tr] == [
        ("SELL", "5", "652.64", "e1", "order:e1"), ("BUY", "5", "650.90", "s3", "order:s3")]
    AL.sync(env.settings, env.con, ad, ACCT, risk_cfg=RISK, manual_cfg=MAN)
    close = env.con.execute("SELECT exit_kind, realized_usd FROM manual_trades WHERE effect='CLOSE'").fetchone()
    assert close["exit_kind"] == "STOP" and Decimal(close["realized_usd"]) == Decimal("8.70")     # (652.64 − 650.90) × 5
    types = {r[0] for r in env.con.execute("SELECT type FROM discipline_events")}
    assert types == {"UNPLANNED"}            # 没有事前计划：只记"无计划成交"；放宽止损要有计划才判
    mods = env.con.execute("SELECT COUNT(*) FROM manual_order_events WHERE broker_order_id='s3'"
                           " AND event_type='MODIFIED'").fetchone()[0]
    assert mods == 2                          # 改单的证据已入库，有计划时即可据此判定
