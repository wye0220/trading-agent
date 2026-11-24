import copy
from decimal import Decimal

import pytest
import yaml

import helpers as H
from trading_agent import db
from trading_agent.config import ConfigError
from trading_agent.engine import config as EC, risk as R

RAW = yaml.safe_load((H.TEST_PROJECT / "config" / "risk.yaml").read_text(encoding="utf-8"))
ERAW = yaml.safe_load((H.TEST_PROJECT / "config" / "engine.yaml").read_text(encoding="utf-8"))
NOW = 1_800_000_000
DAY = "2025-11-25"


def cfg(**over):
    raw = copy.deepcopy(RAW)
    for path, v in over.items():
        node = raw
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = v
    return EC.parse_risk(raw)


def funded(**over):
    base = dict(fees__amount_usd=5, account__approved=True, account__budget_per_trade_usd=500,
                account__daily_loss_limit_usd=1000, account__max_contracts=5)
    base.update(over)
    return cfg(**base)


def state(**kw):
    kw.setdefault("fetched_at_utc", NOW - 10)
    return R.AccountState("acct", DAY, **kw)


# ---------------------------------------------------------------- 1.3 配置
def test_default_risk_config_is_unconfigured():
    c = EC.load_risk(H.TEST_PROJECT)
    assert c.fee_amount_usd is None and not c.fees_configured and c.round_trip_fee_usd == 0
    assert c.research_risk_unit_usd is None and c.paper_quantity == 1
    assert (c.entry_slippage_ticks, c.stop_slippage_ticks, c.slippage_budget_ticks) == (0, 1, 1)
    a = c.account
    assert a.approved is False and not a.configured and a.max_stops_per_day == 2 and a.snapshot_max_age_seconds == 120
    assert a.budget_per_trade_usd is None and a.daily_loss_limit_usd is None and a.max_contracts is None
    assert c.version.startswith("risk-")


def test_default_engine_config():
    e = EC.load_engine(H.TEST_PROJECT)
    assert (e.flatten_minutes_before_close, e.replay_activation_delay_seconds, e.max_explanation_retries) == (5, 0, 3)
    assert set(e.token_budgets) == set(EC.KINDS)


@pytest.mark.parametrize("path,value", [
    ("fees__mode", "per_trade"), ("fees__amount_usd", -1), ("fees__amount_usd", "abc"),
    ("slippage__stop_ticks", -1), ("slippage__entry_ticks", 1.5), ("paper__quantity", 0),
    ("paper__research_risk_unit_usd", 0), ("account__approved", "yes"), ("account__max_contracts", 0),
    ("account__max_stops_per_day", 0)])
def test_invalid_risk_config(path, value):
    with pytest.raises(ConfigError):
        cfg(**{path: value})


def test_invalid_engine_config():
    for mutate in (lambda r: r.update(flatten_minutes_before_close=-1), lambda r: r["token_budgets"].pop("Candidate"),
                   lambda r: r["explanations"].update(max_retries=0), lambda r: r.pop("explanations")):
        raw = copy.deepcopy(ERAW)
        mutate(raw)
        with pytest.raises(ConfigError):
            EC.parse_engine(raw)


def test_risk_version_tracks_content():
    assert cfg().version == cfg().version
    assert cfg(slippage__budget_ticks=2).version != cfg().version
    assert cfg(fees__amount_usd=5).version != cfg().version


# ---------------------------------------------------------------- 4.1 每手风险
def test_planned_risk_es_example(env):
    es = env.settings.instrument("ES")
    assert R.planned_risk_per_contract(es, 16, cfg(fees__amount_usd=5)) == Decimal("217.5")
    per_side = cfg(fees__mode="per_side", fees__amount_usd="2.5")
    assert per_side.round_trip_fee_usd == 5 and R.planned_risk_per_contract(es, 16, per_side) == Decimal("217.5")
    assert R.planned_risk_per_contract(es, 16, cfg()) == Decimal("212.5")           # 费用未配置按 0
    assert R.planned_risk_per_contract(env.settings.instrument("MES"), 16, cfg(fees__amount_usd=5)) == Decimal("26.25")
    with pytest.raises(ValueError):
        R.planned_risk_per_contract(es, 0, cfg())


# ---------------------------------------------------------------- 4.2 手数
def test_size_for_budget():
    r = Decimal("217.5")
    assert R.size_for_budget(r, Decimal(500), 5, Decimal(10_000)) == 2
    assert R.size_for_budget(r, Decimal(200), 5, Decimal(10_000)) == 0
    assert R.size_for_budget(r, Decimal(5000), 5, Decimal(10_000)) == 5              # 受最大手数约束
    assert R.size_for_budget(r, Decimal(500), 5, Decimal(300)) == 1                  # 受当日剩余风险约束
    assert R.size_for_budget(r, Decimal(500), 5, Decimal(-50)) == 0


def test_paper_risk_ignores_account_budget(env):
    es = env.settings.instrument("ES")
    d = R.paper_risk(es, 16, cfg())                       # 账户预算未配置、未批准
    assert (d.status, d.quantity, d.reason_code, d.risk_per_contract_usd) == ("PASS", 1, None, Decimal("212.5"))


# ---------------------------------------------------------------- 4.3 人工计划核验
def test_manual_plan_reasons(env):
    es = env.settings.instrument("ES")
    chk = lambda c, st, stop=16, q=None: R.check_manual_plan(es, stop, c, st, NOW, q)          # noqa: E731
    assert chk(EC.load_risk(H.TEST_PROJECT), state()).reason_code == "BUDGET_NOT_APPROVED"            # 默认配置
    assert chk(funded(account__approved=False), state()).reason_code == "BUDGET_NOT_APPROVED"
    assert chk(funded(account__max_contracts=None), state()).reason_code == "BUDGET_NOT_APPROVED"
    f = funded()
    ok = chk(f, state())
    assert (ok.status, ok.quantity, ok.risk_per_contract_usd) == ("PASS", 2, Decimal("217.5"))
    assert chk(f, state(), q=2).quantity == 2 and chk(f, state(), q=1).quantity == 1
    over = chk(f, state(), q=3)
    assert (over.status, over.reason_code, over.quantity) == ("BLOCKED", "RISK_OVER_BUDGET", 2)
    assert chk(f, state(), stop=None).reason_code == "NO_STOP"
    small = chk(funded(account__budget_per_trade_usd=200), state())
    assert (small.status, small.reason_code, small.quantity) == ("BLOCKED", "MIN_SIZE_OVER_BUDGET", 0)
    assert chk(f, state(realized_loss_usd=Decimal(1000))).reason_code == "DAILY_LIMIT_HIT"
    assert chk(f, state(realized_loss_usd=Decimal(700))).quantity == 1                           # 剩余 300，只够 1 手
    assert chk(f, state(realized_loss_usd=Decimal(600), reserved_usd=Decimal(300))).reason_code == "DAILY_LIMIT_HIT"
    assert chk(f, state(halted=True)).reason_code == "ACCOUNT_HALTED_TWO_STOPS"
    assert chk(f, state(stops=2)).reason_code == "ACCOUNT_HALTED_TWO_STOPS"


def test_manual_plan_unknown_or_stale(env):
    es = env.settings.instrument("ES")
    f = funded()
    for st in (None, state(fetched_at_utc=None), state(fetched_at_utc=NOW - 121), state(complete=False)):
        d = R.check_manual_plan(es, 16, f, st, NOW)
        assert (d.status, d.reason_code) == ("BLOCKED", "RISK_UNKNOWN_OR_STALE")
    assert R.check_manual_plan(es, 16, f, state(fetched_at_utc=NOW - 120), NOW).status == "PASS"


# ---------------------------------------------------------------- 4.4 预留
def reserve(env, c, plan, qty=1, version=None, **kw):
    es = env.settings.instrument("ES")
    if version is None:
        version = R.account_state(env.con, "acct", DAY).version
    return R.reserve(env.con, es, c, account_id="acct", trading_day=DAY, plan_id=plan, stop_distance_ticks=16,
                     quantity=qty, expected_version=version, now_utc=NOW, fetched_at_utc=NOW - 5, **kw)


def test_reserve_only_one_of_two_plans_fits(env):
    c = funded(account__daily_loss_limit_usd=300)            # 只够一份 217.5 的计划
    a = reserve(env, c, "p1")
    assert a.status == "PASS" and R.account_state(env.con, "acct", DAY).reserved_usd == Decimal("217.5")
    b = reserve(env, c, "p2")
    assert (b.status, b.reason_code) == ("BLOCKED", "DAILY_LIMIT_HIT")
    st = R.account_state(env.con, "acct", DAY)
    assert st.reserved_usd == Decimal("217.5") and st.version == 1
    assert [r[0] for r in env.con.execute("SELECT plan_id FROM risk_reservations")] == ["p1"]
    again = reserve(env, c, "p1")                            # 同一计划重复预留是空操作
    assert again.status == "PASS" and again.detail["idempotent"] and R.account_state(env.con, "acct", DAY).version == 1
    assert R.release(env.con, "p1") is True and R.release(env.con, "p1") is False
    assert R.account_state(env.con, "acct", DAY).reserved_usd == 0
    assert reserve(env, c, "p2").status == "PASS"


def test_reserve_rejects_stale_version(env):
    c = funded()
    reserve(env, c, "p1")
    before = dict(env.con.execute("SELECT * FROM account_risk_days").fetchone())
    with pytest.raises(db.StaleVersion):
        reserve(env, c, "p2", version=0)                     # 预检时看到的是旧状态
    assert dict(env.con.execute("SELECT * FROM account_risk_days").fetchone()) == before
    assert env.con.execute("SELECT COUNT(*) FROM risk_reservations").fetchone()[0] == 1
    assert not env.con.in_transaction


def test_reserve_blocked_when_not_approved(env):
    d = reserve(env, EC.load_risk(H.TEST_PROJECT), "p1")
    assert d.reason_code == "BUDGET_NOT_APPROVED"
    assert env.con.execute("SELECT COUNT(*) FROM account_risk_days").fetchone()[0] == 0


# ---------------------------------------------------------------- 4.5 账户止损计数
def exit_(env, c, sid, reason, net, family="pa_brooks"):
    with env.con:
        return R.record_account_exit(env.con, c, account_id="acct", trading_day=DAY, source_id=sid,
                                     exit_reason=reason, net_usd=Decimal(net), strategy_family=family)


def test_account_two_stops_shared_across_strategies(env):
    es, c = env.settings.instrument("ES"), funded()
    assert exit_(env, c, "f1", "STOP", -200) is True
    assert exit_(env, c, "f1", "STOP", -200) is False                 # 同一笔成交重复导入：只计一次
    st = R.account_state(env.con, "acct", DAY)
    assert (st.stops, st.halted, st.realized_loss_usd) == (1, False, Decimal(200))
    exit_(env, c, "f2", "STOP", -150, family="pa_brooks")
    st = R.account_state(env.con, "acct", DAY, fetched_at_utc=NOW - 1)
    assert (st.stops, st.halted, st.realized_loss_usd) == (2, True, Decimal(350))
    # 换一个策略家族来请求：账户仍然停手
    d = R.check_manual_plan(es, 16, c, st, NOW)
    assert (d.status, d.reason_code) == ("BLOCKED", "ACCOUNT_HALTED_TWO_STOPS")
    blocked = R.reserve(env.con, es, c, account_id="acct", trading_day=DAY, plan_id="orb-plan", stop_distance_ticks=16,
                        quantity=1, expected_version=st.version, now_utc=NOW, fetched_at_utc=NOW - 1,
                        strategy_family="orb")
    assert blocked.reason_code == "ACCOUNT_HALTED_TWO_STOPS"
    exit_(env, c, "f3", "TARGET", 500, family="orb")                  # 停手之后的盈利不解除
    assert R.account_state(env.con, "acct", DAY).halted is True
    assert R.account_state(env.con, "acct", "2025-11-26").halted is False     # 次日是新的记录


def test_account_stop_counter_reset_rules(env):
    c = funded()
    exit_(env, c, "a", "STOP", -100)
    exit_(env, c, "b", "TARGET", 300)                                  # 净盈利清零
    exit_(env, c, "c", "STOP", -100)
    assert R.account_state(env.con, "acct", DAY).stops == 1
    exit_(env, c, "d", "OTHER", 0)                                     # 保本不清零
    exit_(env, c, "e", "OTHER", -20)                                   # 亏损不清零
    assert R.account_state(env.con, "acct", DAY).stops == 1
    exit_(env, c, "f", "STOP", -100)
    assert R.account_state(env.con, "acct", DAY).halted is True


# ---------------------------------------------------------------- 4.6 结算
def test_settle_trade(env):
    es = env.settings.instrument("ES")
    c = cfg(fees__amount_usd=5, paper__research_risk_unit_usd=200)
    t = R.settle_trade(es, c, side="LONG", quantity=1, entry_ticks=20000, exit_ticks=20033, initial_stop_ticks=19984)
    assert (t["pnl_ticks"], t["gross_usd"], t["fees_usd"], t["net_usd"]) == (33, Decimal("412.5"), 5, Decimal("407.5"))
    assert t["r_research"] == Decimal("407.5") / 200 and t["r_initial"] == Decimal("407.5") / 200 and t["flags"] == ()
    two = R.settle_trade(es, cfg(fees__amount_usd=0, paper__research_risk_unit_usd=200), side="LONG", quantity=1,
                         entry_ticks=20000, exit_ticks=20032, initial_stop_ticks=19984)
    assert two["net_usd"] == 400 and two["r_research"] == 2 and two["net_sign"] == 1
    s = R.settle_trade(es, c, side="SHORT", quantity=1, entry_ticks=20000, exit_ticks=20016, initial_stop_ticks=20016)
    assert (s["pnl_ticks"], s["net_usd"], s["net_sign"], s["initial_risk_ticks"]) == (-16, Decimal(-205), -1, 16)


def test_settle_flags_when_unconfigured(env):
    es = env.settings.instrument("ES")
    t = R.settle_trade(es, cfg(), side="LONG", quantity=1, entry_ticks=20000, exit_ticks=20000,
                       initial_stop_ticks=19984, flags=("AMBIGUOUS_ENTRY",))
    assert t["fees_usd"] == 0 and t["net_sign"] == 0 and t["r_research"] is None and t["r_initial"] == 0
    assert t["flags"] == ("AMBIGUOUS_ENTRY", "FEES_NOT_CONFIGURED", "RESEARCH_RISK_UNIT_NOT_SET")


def test_gap_loss_exceeds_one_r(env):
    es = env.settings.instrument("ES")
    t = R.settle_trade(es, cfg(fees__amount_usd=0), side="LONG", quantity=1, entry_ticks=20000, exit_ticks=19959,
                       initial_stop_ticks=19984, flags=("GAP_THROUGH_STOP",))
    assert t["r_initial"] < -1 and "GAP_THROUGH_STOP" in t["flags"]


# ---------------------------------------------------------------- 2.1 migration
def test_migration_0002_on_existing_db(tmp_path):
    from datetime import date
    st = H.make_settings(tmp_path)
    con = db.connect(st.runtime_db)
    con.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at_utc INTEGER NOT NULL)")
    con.executescript("BEGIN;\n" + (db.MIGRATIONS / "runtime" / "0001_initial.sql").read_text(encoding="utf-8") + "\nCOMMIT;")
    con.execute("INSERT INTO schema_migrations VALUES ('0001_initial', 1)")
    con.execute("INSERT INTO bars (source, contract_id, timeframe, bar_start_utc, revision, instrument_root, bar_end_utc,"
                " trading_day, session_type, open, high, low, close, volume, available_at_utc, available_at_estimated)"
                " VALUES ('x','ESZ2025','5m',1,1,'ES',301,'2025-11-25','RTH',1,2,0,1,5,306,1)")
    con.commit()
    assert "engine_runs" not in db.tables(con)
    assert db.migrate(con, "runtime") == ["0002_engine", "0003_manual", "0004_research", "0005_review", "0006_evolve", "0007_memory", "0008_evaluation", "0009_release", "0010_ops"]
    t = db.tables(con)
    assert {"engine_runs", "paper_positions", "paper_trades", "explanation_requests", "account_risk_days",
            "account_risk_events", "risk_reservations"} <= t
    assert con.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1
    assert db.applied_migrations(con) == ["0001_initial", "0002_engine", "0003_manual", "0004_research", "0005_review", "0006_evolve", "0007_memory", "0008_evaluation", "0009_release", "0010_ops"]
    assert {"plan_events", "discipline_events", "watch_state"} <= db.tables(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(paper_orders)")}
    assert {"armed_at_utc", "expires_at_utc", "signal_bar_end_utc", "trigger_ticks", "limit_ticks", "stop_ticks",
            "target_json", "flags_json"} <= cols
    con.close()
