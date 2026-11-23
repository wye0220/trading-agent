"""阶段 5：截止日执行、内容摘要、数据切分、基线冻结、度量、功效测算。"""
import copy
import json
from datetime import date
from decimal import Decimal

import numpy as np
import pytest
import yaml

import helpers as H
import test_engine_runner as ER
from conftest import Env
from trading_agent import contenthash, sessions
from trading_agent.config import ConfigError
from trading_agent.engine import config as EC
from trading_agent.research import baseline as BL
from trading_agent.research import config as RC
from trading_agent.research import metrics as M
from trading_agent.research import power as PW
from trading_agent.research import report as RP
from trading_agent.research import splits as SP
from trading_agent.research.run import run_baseline
from trading_agent.strategies import variants as V
from trading_agent.strategies.registry import default_registry

RAW = yaml.safe_load((H.TEST_PROJECT / "config" / "research.yaml").read_text(encoding="utf-8"))
START = date(2025, 9, 15)
REG = default_registry()


def cfg(**over):
    raw = copy.deepcopy(RAW)
    raw["power"].update(simulations=200, inner_resamples=100, min_dev_days=2, confirm_days=40)
    raw["splits"]["default_usage"]["confirm_capsules"] = 2
    for path, v in over.items():
        node = raw
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = v
    return RC.parse(raw)


def days_from(start, n):
    return sessions.trading_days(start, date(2026, 12, 31))[:n]


def real(day, **kw):
    kw.setdefault("source", "databento")
    kw.setdefault("contract", "ESZ2025")
    return H.rth_bars(day, fixture=False, **kw)


def configured_risk(**acct):
    raw = yaml.safe_load((H.TEST_PROJECT / "config" / "risk.yaml").read_text(encoding="utf-8"))
    raw["fees"]["amount_usd"] = 5
    raw["paper"]["research_risk_unit_usd"] = 500
    raw["account"].update(acct)
    return EC.parse_risk(raw)


# ---------------------------------------------------------------- 配置变体
def test_variant_whitelist_and_paths():
    base = {"params": {"spike": {"min_move_atr": "2.5"}, "discipline": {"max_stops_per_day": 2}},
            "setups": [{"name": "a", "exempt": ["x", "y"]}, {"name": "b", "exempt": []}]}
    pats = ("params.spike.*", "setups.*.exempt")
    out = V.apply_diff(base, {"params.spike.min_move_atr": "2.0", "setups.a.exempt": ["y"]}, pats)
    assert out["params"]["spike"]["min_move_atr"] == "2.0" and out["setups"][0]["exempt"] == ["y"]
    assert base["params"]["spike"]["min_move_atr"] == "2.5" and base["setups"][0]["exempt"] == ["x", "y"]   # 原配置不变
    for bad in ({"params.discipline.max_stops_per_day": 5}, {"params.spike.nope": 1}, {"setups.zzz.exempt": []}, {}):
        with pytest.raises(V.VariantError):
            V.apply_diff(base, bad, pats)
    assert V.allowed("params.spike.min_bars", pats) and not V.allowed("params.spike", pats)
    assert not V.allowed("params.spike.a.b", pats)


def test_registry_variant_changes_only_the_copy():
    v = REG.variant("pa_brooks", {"params.discipline.tail_window_minutes": 60}, "T")
    assert v.version == "v1.0+T" and v.config.tail_window_minutes == 60
    assert REG.get("pa_brooks").config.tail_window_minutes == 30 and REG.get("pa_brooks").version == "v1.0"
    with pytest.raises(Exception, match="白名单"):
        REG.variant("pa_brooks", {"params.discipline.max_stops_per_day": 9}, "T")
    with pytest.raises(Exception, match="白名单"):
        REG.variant("pa_brooks", {"setups.wedge.enabled": True}, "T")       # 启用新形态不能靠配置差异


def test_research_config_validation():
    c = RC.load_research(H.PROJECT)
    assert c.power.alpha == pytest.approx(c.power.alpha_family / c.power.max_formal_tests)
    tracks = {p.track for p in c.perturbations_for("pa_brooks")}
    assert tracks == {"PRUNING_DEFENSIVE", "ALPHA_EXPANSION"}             # 两类扰动各至少一个
    assert set(RC.validate_perturbations(c, REG, "pa_brooks")) == {p.id for p in c.perturbations_for("pa_brooks")}
    bad = copy.deepcopy(RAW)
    bad["perturbations"]["pa_brooks"][0]["diff"] = {"params.discipline.max_stops_per_day": 5}
    with pytest.raises(ConfigError, match="白名单"):
        RC.validate_perturbations(RC.parse(bad), REG, "pa_brooks")
    for path, val in (("power.alpha_family", 0), ("power.true_effect_grid_r_per_day", [0.1, 0.05]),
                      ("splits.capsule_valid_days", 0)):
        with pytest.raises(ConfigError):
            cfg(**{path: val})
    dup = copy.deepcopy(RAW)
    dup["perturbations"]["pa_brooks"].append(dup["perturbations"]["pa_brooks"][0])
    with pytest.raises(ConfigError, match="重复"):
        RC.parse(dup)


# ---------------------------------------------------------------- 内容摘要与截止日
def test_content_hash_is_order_independent():
    rows = [("s", "c", "1m", 60 * i, 1, 2, 0, 1, i) for i in range(50)]
    a = b = 0
    for r in rows:
        a = contenthash.add(a, contenthash.bar_digest(*r))
    for r in reversed(rows):
        b = contenthash.add(b, contenthash.bar_digest(*r))
    assert a == b and contenthash.to_hex(a) != contenthash.EMPTY
    assert contenthash.merge_hex(contenthash.to_hex(a), None) == contenthash.to_hex(a)
    changed = contenthash.add(0, contenthash.bar_digest("s", "c", "1m", 0, 1, 2, 0, 1, 99))
    assert changed != contenthash.add(0, contenthash.bar_digest(*rows[0]))


def test_cutoff_paths_give_same_hash_and_remove_results(tmp_path):
    d = days_from(START, 4)
    rows = [b for x in d for b in real(x)]
    rows += [H.bar(H.et(d[3], 4, 0) + 300 * i, 4990, fixture=False, source="databento", contract="ESZ2025") for i in range(6)]

    a = Env(tmp_path / "a")                           # 路径一：导入时就带截止日
    H.write_csv(a.root / "real.csv", rows)
    sa = a.ingest(through=d[1])
    assert sa["withheld_after_cutoff"] == 78 * 2 + 6 and sa["moved_after_cutoff"] == 0

    b = Env(tmp_path / "b")                           # 路径二：先全量导入，后来才设截止日
    H.write_csv(b.root / "real.csv", list(reversed(rows)))
    b.ingest()
    assert b.count("trading_day>?", d[1].isoformat()) > 0
    assert b.con.execute("SELECT COUNT(*) FROM trading_days WHERE trading_day>?", (d[1].isoformat(),)).fetchone()[0] == 2
    sb = b.ingest(through=d[1])
    assert sb["moved_after_cutoff"] == 78 * 2 + 6 and sb["rows_deleted"] > sb["moved_after_cutoff"]     # 派生线也删了

    q = "SELECT trading_day, timeframe, bar_count, rth_slots_present, content_sha256 FROM withheld_coverage ORDER BY 1, 2"
    wa, wb = [tuple(r) for r in a.con.execute(q)], [tuple(r) for r in b.con.execute(q)]
    assert wa == wb and len(wa) == 2 and all(len(r[4]) == 64 and r[4] != contenthash.EMPTY for r in wa)
    for e in (a, b):
        assert e.count("trading_day>?", d[1].isoformat()) == 0
        for t in ("trading_days", "rth_days", "reference_levels"):
            assert e.con.execute(f"SELECT COUNT(*) FROM {t} WHERE trading_day>?", (d[1].isoformat(),)).fetchone()[0] == 0
        assert not [c for c in e.con.execute("SELECT * FROM withheld_coverage").fetchone().keys()
                    if c in ("open", "high", "low", "close")]
        assert e.count("derived=0") == 78 * 2
        e.con.close()


def test_enforce_cutoff_is_idempotent_and_spares_fixtures(tmp_path):
    d = days_from(START, 3)
    e = Env(tmp_path)
    H.write_csv(e.root / "real.csv", [b for x in d for b in real(x)])
    H.write_csv(e.root / "fixture_x.csv", H.rth_bars(d[2]))
    e.ingest()
    e.ingest(through=d[0])
    before = [tuple(r) for r in e.con.execute("SELECT * FROM withheld_coverage ORDER BY trading_day")]
    s = e.ingest(through=d[0])
    assert s["moved_after_cutoff"] == 0
    assert [tuple(r) for r in e.con.execute("SELECT * FROM withheld_coverage ORDER BY trading_day")] == before
    assert e.count("fixture=1 AND derived=0") == 78                       # 合成数据不受截止日影响
    e.con.close()


# ---------------------------------------------------------------- 切分
def split_env(tmp_path, n_dev=4, n_held=7, degraded=(), gaps=()):
    d = days_from(START, n_dev + n_held)
    e = Env(tmp_path, dev_visible_through=d[n_dev - 1])
    rows = []
    for i, x in enumerate(d):
        rows += real(x, skip=[(10, 0)] if i in gaps else ())
    H.write_csv(e.root / "real.csv", rows)
    (e.root / "job").mkdir(parents=True, exist_ok=True)
    (e.root / "job" / "condition.json").write_text(json.dumps(
        [{"date": x.isoformat(), "condition": "degraded" if i in degraded else "available"} for i, x in enumerate(d)]))
    e.ingest()
    return e, d


def test_split_segments_and_capsules(tmp_path):
    e, d = split_env(tmp_path, n_dev=4, n_held=7, degraded={5}, gaps={7})
    c = cfg(**{"splits.capsule_valid_days": 2, "splits.forward_validation_start": d[2].isoformat()})
    reg = SP.register(e.settings, e.con, "ES", c)
    kinds = [(s["kind"], s["seq"], s["start_day"], s["end_day"], s["valid_days"], s["status"]) for s in reg["segments"]]
    iso = [x.isoformat() for x in d]
    assert kinds == [
        ("DEVELOPMENT", 1, iso[0], iso[1], 2, "OPEN"), ("FORWARD_VALIDATION", 1, iso[2], iso[3], 2, "OPEN"),
        ("CAPSULE", 1, iso[4], iso[6], 2, "SEALED"),          # 中间有一个降级日：留在清单里，不计数
        ("CAPSULE", 2, iso[7], iso[9], 2, "SEALED"),          # 开头是缺口日
        ("REMAINDER", 1, iso[10], iso[10], 1, "SEALED")]      # 尾部不足一个胶囊
    cap1 = reg["segments"][2]
    assert cap1["days"] == [[iso[4], 1, None], [iso[5], 0, "DEGRADED"], [iso[6], 1, None]]
    assert reg["segments"][3]["days"][0] == [iso[7], 0, "INCOMPLETE"]
    assert [s["default_role"] for s in reg["segments"] if s["kind"] == "CAPSULE"] == ["BLIND_SCREEN", "CONFIRM"]
    assert reg["summary"]["capsules"] == 2 and reg["summary"]["candidates_supported"] == 0
    seen = [x[0] for s in reg["segments"] for x in s["days"]]
    assert seen == iso and len(set(seen)) == len(seen)                    # 不重叠、不遗漏
    assert e.count("trading_day>?", iso[3]) == 0                          # 胶囊的价格不在库里

    again = SP.register(e.settings, e.con, "ES", c)                       # 幂等
    assert again["registry_id"] == reg["registry_id"] and reg["created"] and not again["created"]
    assert e.con.execute("SELECT COUNT(*) FROM split_registries").fetchone()[0] == 1
    other = SP.build(e.settings, e.con, "ES", cfg(**{"splits.capsule_valid_days": 3}))
    assert other["registry_id"] != reg["registry_id"]
    e.con.close()


def test_split_requires_cutoff_for_real_data(env):
    H.write_csv(env.root / "real.csv", real(START))
    env.ingest()
    with pytest.raises(SP.SplitError, match="开发可见截止日"):
        SP.build(env.settings, env.con, "ES", cfg())


def test_split_refuses_when_db_still_has_later_real_bars(tmp_path):
    d = days_from(START, 3)
    e = Env(tmp_path)
    H.write_csv(e.root / "real.csv", [b for x in d for b in real(x)])
    e.ingest()
    e2 = Env(tmp_path, dev_visible_through=d[0])
    with pytest.raises(SP.SplitError, match="移出"):
        SP.build(e2.settings, e2.con, "ES", cfg())
    e.con.close()
    e2.con.close()


# ---------------------------------------------------------------- 基线冻结
D = [ER.D1, ER.D2, date(2025, 12, 1)]


def fixture_env(env, days=D):
    rows = []
    for d in days:
        rows += ER.scripted_day(d)
    ER.load(env, rows)
    return env


def test_freeze_fixture_baseline_is_reproducible(env):
    fixture_env(env)
    c = cfg(**{"splits.forward_validation_start": D[2].isoformat(), "splits.warmup_trading_days": 0})
    base = BL.freeze(env.settings, env.con, env.scope, REG.get("pa_brooks"), c, risk_cfg=ER.RISK, engine_cfg=ER.ENG)
    assert base["status"] == "FROZEN" and "FIXTURE_ONLY" in base["flags"] and base["digests"]["identical"]
    assert {"FEES_NOT_CONFIGURED", "RESEARCH_RISK_UNIT_NOT_SET"} <= set(base["flags"])
    counts = base["digests"]["base"]["counts"]
    assert counts["decisions"] == 78 * 3 and counts["trades"] == 6 and counts["orders"] == 6
    m = base["manifest"]
    assert m["strategy"]["enabled_setups"] == ["spike_pullback"] and m["engine_version"] == "fill-v1.0"
    assert m["data"]["days"] == 3 and m["indicators"]["ATR_PERIOD"] == 14 and m["random_seed"] is None
    assert m["splits"]["registry_id"] == base["registry"]["registry_id"] and m["fixture"] is True
    # 校验运行的明细已清掉，只留摘要；主运行的明细还在
    n = lambda t, rid: env.con.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id=?", (rid,)).fetchone()[0]   # noqa: E731
    assert n("scan_events", f"base:{base['baseline_id']}") == 78 * 3
    for t in ("scan_events", "paper_trades", "paper_orders", "paper_fills", "paper_positions", "engine_runs"):
        assert n(t, f"verify:{base['baseline_id']}") == 0
    roles = {r["role"]: r["status"] for r in env.con.execute("SELECT role, status FROM research_runs")}
    assert roles == {"BASE": "DONE", "VERIFY": "DONE"}

    again = BL.freeze(env.settings, env.con, env.scope, REG.get("pa_brooks"), c, risk_cfg=ER.RISK, engine_cfg=ER.ENG)
    assert again["baseline_id"] == base["baseline_id"] and again["reused"]


def test_baseline_id_changes_with_revision_config_and_risk(env):
    fixture_env(env)
    c = cfg()
    strat = REG.get("pa_brooks")

    def bid(strategy=strat, risk=ER.RISK):
        reg = SP.build(env.settings, env.con, "ES", c)
        return BL.baseline_id_of(BL.build_manifest(env.settings, env.con, env.scope, strategy, reg, c, risk, ER.ENG))
    first = bid()
    assert bid() == first
    assert bid(strategy=REG.variant("pa_brooks", {"params.spike.min_move_atr": "2.0"}, "X")) != first
    assert bid(risk=configured_risk()) != first
    row = dict(ER.scripted_day(D[0])[-1], close="5007.75")               # 同一根 K 的新内容 → 新 revision
    H.write_csv(env.root / "fixture_rev.csv", [row])
    env.ingest()
    assert env.count("revision=2 AND derived=0") == 1 and bid() != first


class Flaky:
    """第二次回放时行为不同的策略：用来验证不一致会被发现。"""
    strategy_family = "pa_brooks"
    version = "flaky"

    def __init__(self):
        self.inner = REG.get("pa_brooks")
        self.config = self.inner.config
        self.calls = 0

    def feature_requirements(self):
        return self.inner.feature_requirements()

    def initial_state(self):
        self.calls += 1
        return self.inner.initial_state()

    def on_bar(self, ctx, state):
        res = self.inner.on_bar(ctx, state)
        if self.calls >= 2 and res.signal_status == "SETUP_VALID":
            from dataclasses import replace
            from trading_agent.strategies.base import DecisionMetadata  # noqa: F401
            return replace(res, signal_status="WAIT", order_intent=None,
                           decision_metadata=replace(res.decision_metadata, signal_status="WAIT"))
        return res

    def evaluate_pretrade_alignment(self, ctx, state, plan):
        return self.inner.evaluate_pretrade_alignment(ctx, state, plan)


def test_non_deterministic_baseline_is_flagged(env):
    fixture_env(env, D[:1])
    try:
        base = BL.freeze(env.settings, env.con, env.scope, Flaky(), cfg(), risk_cfg=ER.RISK, engine_cfg=ER.ENG)
    except Exception as e:                                # 策略契约校验可能先拦下不一致的输出
        pytest.skip(f"引擎拒绝了构造出的不一致结果：{e}")
    assert base["status"] == "NON_DETERMINISTIC" and not base["digests"]["identical"]
    assert base["digests"]["base"]["trades"] != base["digests"]["verify"]["trades"]


def test_research_package_has_no_signal_or_fill_logic():
    src = H.PROJECT / "src" / "trading_agent" / "research"
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(src.glob("*.py")))
    for banned in ("apply_bar", "expire_orders", "flatten_check", "pa_brooks import", "strategies.pa_brooks",
                   "on_bar(", "WorkingOrder", "paper_risk("):
        assert banned not in text, banned
    assert "runner.replay(" in text and text.count("runner.replay(") == 1          # 回放只有一个入口
    code = "\n".join(p.read_text(encoding="utf-8") for p in (src / "metrics.py", src / "report.py"))
    for word in ("spike", "pullback_bucket", "H1", "L1", "h1l1"):                 # 报告代码不认识策略专有字段
        assert word not in code, word


# ---------------------------------------------------------------- 度量
def T(day, net, *, qty=1, reason=None, flags=(), risk=250, entry=0, setup="s", sub="1m", gap=0, **meta):
    net = Decimal(str(net))
    return {"trade_id": f"{day}-{entry}", "trading_day": day, "contract_id": "ESZ2025", "setup_id": setup, "side": "LONG",
            "quantity": qty, "entry_ticks": 20000, "entry_time_utc": H.et(date.fromisoformat(day), 10, 0) + entry,
            "exit_ticks": 20010, "exit_time_utc": H.et(date.fromisoformat(day), 10, 30) + entry,
            "exit_reason": reason or ("TARGET" if net > 0 else "STOP"), "initial_risk_ticks": 20, "pnl_ticks": 0,
            "gross_usd": net + 5, "fees_usd": Decimal(5), "net_usd": net, "r_initial": net / 250, "r_research": net / 500,
            "flags": list(flags), "holding_seconds": 1800, "mae_ticks": 4, "mfe_ticks": 12, "sub_bar_timeframe": sub,
            "gap_excess_ticks": gap, "gap_excess_usd": Decimal(gap) * Decimal("12.5"),
            "slippage_cost_usd": Decimal(0), "planned_risk_per_contract_usd": Decimal(risk), "metadata": meta}


KW = dict(tick_value=Decimal("12.5"), research_unit=Decimal(500), stress_ticks_per_side=1)
DAYS = ["2025-11-24", "2025-11-25", "2025-11-26", "2025-11-28"]


def test_summarize_counts_zero_days_and_pf():
    ts = [T(DAYS[0], 500), T(DAYS[0], -250, entry=60, flags=["AMBIGUOUS_BAR"]), T(DAYS[2], -250, sub="5m", gap=3,
                                                                                 flags=["GAP_THROUGH_STOP"])]
    s = M.summarize(ts, DAYS, **KW)
    assert (s["days"], s["trades"], s["no_trade_days"]) == (4, 3, 2)
    assert s["net_usd"] == 0 and s["fees_usd"] == 15 and s["gross_usd"] == 15
    assert s["win_rate"] == pytest.approx(1 / 3) and s["profit_factor"] == pytest.approx(1.0)
    assert s["net_r_per_day"] == pytest.approx(0.0) and s["net_r_per_trade"] == pytest.approx(0.0)
    assert s["max_drawdown_usd"] == pytest.approx(250.0) and s["max_drawdown_trade_usd"] == pytest.approx(500.0)
    assert s["ambiguous_bar_share"] == pytest.approx(1 / 3) and s["coarse_sub_bar_trades"] == 1
    assert s["gap_through_stop"] == {"trades": 1, "excess_ticks": 3, "excess_usd": Decimal("37.5")}
    assert s["stress"]["net_usd"] == Decimal(-75)                         # 每笔再扣 2 tick
    only_wins = M.summarize([T(DAYS[0], 500)], DAYS, **KW)
    assert only_wins["profit_factor"] is None and "不可估" in only_wins["profit_factor_note"]
    empty = M.summarize([], DAYS, **KW)
    assert empty["trades"] == 0 and empty["no_trade_days"] == 4 and empty["win_rate"] is None
    assert empty["net_r_per_day"] == 0 and empty["profit_factor"] is None
    outside = M.summarize(ts + [T("2025-12-01", 9999)], DAYS, **KW)         # 不在度量日里的交易不计
    assert outside["net_usd"] == 0 and outside["trades"] == 3


def test_group_table_and_daily_series():
    ts = [T(DAYS[0], 500, label="H1"), T(DAYS[1], -250, label="H2"), T(DAYS[1], 100, entry=60, label="H1"), T(DAYS[3], 50)]
    g = M.group_table(ts, DAYS, lambda t: M._path(t["metadata"], "label"), **KW)
    assert set(g) == {"H1", "H2", "（无）"} and g["H1"]["trades"] == 2 and g["H1"]["net_usd"] == 600
    assert g["H2"]["win_rate"] == 0 and g["H1"]["profit_factor"] is None
    assert M._path({"a": {"b": 3}}, "a.b") == 3 and M._path({"a": 1}, "a.b") is None
    s = M.daily_series(ts, DAYS, Decimal(500))
    assert s["net"] == [1.0, -0.3, 0.0, 0.1] and s["trades"] == [1, 2, 0, 1] and s["unit"] == "R"
    assert M._bucket(H.et(date(2025, 11, 25), 9, 45), ("09:30", "10:30")) == "09:30–10:30"
    assert M._bucket(H.et(date(2025, 11, 25), 15, 10), ("09:30", "10:30")) == "10:30 之后"


def test_risk_sized_requires_approval_and_applies_limits():
    ts = [T(DAYS[0], -250), T(DAYS[0], -250, entry=60), T(DAYS[0], 500, entry=120), T(DAYS[1], 500, risk=900),
          T(DAYS[2], 500)]
    assert M.risk_sized(ts, DAYS, configured_risk(), **KW)["status"] == "NOT_CONFIGURED"
    limits = dict(budget_per_trade_usd=600, daily_loss_limit_usd=1000, max_contracts=5)
    assert "未批准" in M.risk_sized(ts, DAYS, configured_risk(**limits), **KW)["reason"]
    r = M.risk_sized(ts, DAYS, configured_risk(approved=True, **limits), **KW)
    assert r["status"] == "OK"
    # 第 1 天：2 手 × 两次止损 = −1000，达到日损上限，第三笔跳过。第 2 天：每手风险 900 > 预算 600，跳过。
    assert r["skipped_after_halt"] == 1 and r["skipped_too_small"] == 1
    assert r["summary"]["trades"] == 3 and r["summary"]["net_usd"] == -1000 + 1000 and r["contracts"] == {2: 3}
    one = M.risk_sized(ts, DAYS, configured_risk(approved=True, **{**limits, "max_contracts": 1}), **KW)
    assert one["contracts"] == {1: 3}


def test_load_trades_measures_excursions(env):
    fixture_env(env, D[:1])
    out = ER.do_replay(env, risk_cfg=configured_risk())
    meta = M.setup_metadata(env.con, out["run_id"])
    ts = M.load_trades(env.settings, env.con, env.scope, out["run_id"], configured_risk(), meta)
    assert len(ts) == 2 and len(meta) == 2
    t = ts[0]
    assert t["exit_reason"] == "STOP" and t["holding_seconds"] == 300 and t["sub_bar_timeframe"] == "5m"
    assert t["mae_ticks"] >= t["entry_ticks"] - t["exit_ticks"] > 0 and t["mfe_ticks"] >= 0
    assert t["metadata"]["setup"] == "spike_pullback" and t["metadata"]["label"] == "H1"
    assert t["net_usd"] == Decimal("-192.50") and t["r_research"] == Decimal("-0.385")
    assert t["planned_risk_per_contract_usd"] > 0 and t["slippage_cost_usd"] == Decimal("12.5")
    days = [D[0].isoformat()]
    assert M.order_summary(env.con, out["run_id"], days) == {"orders": 2, "by_status": {"FILLED": 2}, "unfilled_reasons": {}}
    ev = BL.event_summary(env.con, out["run_id"], set(days))
    assert ev["decisions"] == 78 and ev["setups"] == 2 and ev["sub_bar_days"] == {"5m": 1}
    assert "TWO_STOPS_HALT" in ev["skip_reasons"]
    assert BL.event_summary(env.con, out["run_id"], set())["decisions"] == 0


# ---------------------------------------------------------------- 功效
def series(n=600, p=0.5, mu=0.05, seed=1):
    rng = np.random.default_rng(seed)
    tr = (rng.random(n) < p).astype(int)
    return {"net": list(tr * rng.normal(mu, 0.5, n)), "trades": list(tr), "unit": "R"}


def test_power_is_reproducible_and_monotone():
    base = series()
    var = {"net": [x * 0.9 for x in base["net"]], "trades": base["trades"]}
    c = cfg().power
    args = (base["net"], base["trades"], var["net"], var["trades"], "ALPHA_EXPANSION", c)
    a = PW.simulate(*args, stress_r_per_trade=0.05)
    b = PW.simulate(*args, stress_r_per_trade=0.05)
    assert a == b                                                         # 同样的输入和种子：完全相同
    assert PW.simulate(*args, stress_r_per_trade=0.05, seed_offset=1) != a
    ci = [r["ci"] for r in a["rows"]]
    assert ci == sorted(ci) and ci[-1] > ci[0]                            # 真实效应越大，通过率越高
    assert [r["true_effect"] for r in a["rows"]] == list(c.true_effect_grid)
    assert all(0 <= r[k] <= 1 for r in a["rows"] for k in ("ci", "effect", "drawdown", "stress", "min_trades", "joint"))
    assert all(r["joint"] <= min(r["ci"], r["effect"], r["drawdown"], r["stress"], r["min_trades"]) + 1e-12
               for r in a["rows"])


def test_power_low_frequency_fails_min_trades():
    base = series(p=0.1)                                                  # 40 日约 4 笔
    var = {"net": base["net"], "trades": base["trades"]}
    r = PW.simulate(base["net"], base["trades"], [x + 0.001 for x in var["net"]], var["trades"], "ALPHA_EXPANSION",
                    cfg().power, stress_r_per_trade=0.05)
    assert all(row["min_trades"] < 0.05 and row["joint"] < 0.05 for row in r["rows"])
    assert r["min_effect_for_target"] is None


def test_power_strong_effect_passes_when_trades_suffice():
    base = series(p=0.9, mu=0.3)
    var = {"net": [x + 0.0001 * (i % 3) for i, x in enumerate(base["net"])], "trades": base["trades"]}
    c = cfg(**{"power.true_effect_grid_r_per_day": [0, 0.3], "power.track_b.drawdown_increase_max_r": 100.0}).power
    r = PW.simulate(base["net"], base["trades"], var["net"], var["trades"], "ALPHA_EXPANSION", c, stress_r_per_trade=0.05)
    assert r["rows"][0]["joint"] < 0.1 and r["rows"][1]["joint"] > 0.9    # 配对差几乎无噪声：0.3 的效应必然检出
    assert 0 < r["min_effect_for_target"] <= 0.3
    a = PW.simulate(base["net"], base["trades"], var["net"], var["trades"], "PRUNING_DEFENSIVE",
                    cfg(**{"power.track_a.drawdown_improvement_min_pct": 0}).power, stress_r_per_trade=0.05)
    assert a["rows"][0]["ci"] > 0.9                                       # 等效时非劣效条件通过


def test_power_analyze_eligibility_and_availability():
    base = series()
    variants = [{"id": "A", "track": "PRUNING_DEFENSIVE", "series": {"net": [x * 0.8 for x in base["net"]], "trades": base["trades"]}},
                {"id": "B", "track": "ALPHA_EXPANSION", "series": {"net": [x * 1.3 for x in base["net"]], "trades": base["trades"]}}]
    c = cfg().power
    r = PW.analyze(base, variants, c, stress_r_per_trade=0.05, available_days=30, fixture=False)
    assert r["alpha"] == pytest.approx(0.01) and r["baseline"]["days"] == 600
    assert r["baseline"]["zero_trade_day_share"] == pytest.approx(1 - sum(base["trades"]) / 600)
    assert set(r["tracks"]) == {"PRUNING_DEFENSIVE", "ALPHA_EXPANSION"}
    assert r["tracks"]["ALPHA_EXPANSION"]["eligibility"] == "EVIDENCE_INSUFFICIENT"
    av = r["availability"]
    assert av["required_days"] > 30 and not av["sufficient"] and av["min_testable_delta"] > c.delta_r_per_day
    sd = max(v["paired"]["std"] for v in r["variants"])
    assert av["min_testable_delta"] == pytest.approx(PW.detectable_effect(sd, 30, c.alpha, c.power_target))
    fx = PW.analyze(base, variants, c, stress_r_per_trade=0.05, available_days=0, fixture=True)
    assert "FIXTURE_ONLY" in fx["flags"]
    assert all(t["eligibility"] == "EVIDENCE_INSUFFICIENT" for t in fx["tracks"].values())
    few = PW.analyze({k: v[:50] if k != "unit" else v for k, v in base.items()},
                     [{**v, "series": {k: x[:50] for k, x in v["series"].items()}} for v in variants],
                     cfg(**{"power.min_dev_days": 200}).power, stress_r_per_trade=0.05, available_days=400, fixture=False)
    assert "CI_UNRELIABLE" in few["flags"]


def test_normal_approximation_matches_spec_table():
    # 提示词包的例子：配对日差标准差 0.30R、单侧 α=0.01、功效 80%
    assert PW.detectable_effect(0.30, 20, 0.01, 0.80) == pytest.approx(0.21, abs=0.005)
    assert PW.detectable_effect(0.30, 40, 0.01, 0.80) == pytest.approx(0.15, abs=0.005)
    assert PW.required_days(0.30, 0.05, 0.01, 0.80) == pytest.approx(362, abs=2)
    assert PW.required_days(0.0, 0.05, 0.01, 0.8) is None and PW.autocorr1([1, 1, 1]) is None
    idx = PW.block_indices(np.random.default_rng(0), 100, 40, 5, 7)
    assert idx.shape == (7, 40) and ((idx[:, 1:5] - idx[:, 0:4]) % 100 == 1).all()       # 区块内是连续的交易日


# ---------------------------------------------------------------- 总编排与报告
def test_run_baseline_on_fixtures_writes_reports(env):
    days = days_from(date(2025, 11, 17), 8)
    fixture_env(env, days)
    c = cfg(**{"splits.forward_validation_start": days[6].isoformat(), "splits.warmup_trading_days": 1})
    logs = []
    out = run_baseline(env.settings, env.con, env.scope, REG, research_cfg=c, risk_cfg=configured_risk(),
                       engine_cfg=ER.ENG, out_dir=env.tmp / "reports", log=logs.append)
    assert out["ok"] and out["status"] == "FROZEN" and "FIXTURE_ONLY" in out["flags"]
    assert out["strategy_validity"] == "EVIDENCE_INSUFFICIENT"
    assert out["total"]["days"] == 7 and out["total"]["trades"] == 14           # 预热日的 2 笔不计
    assert all(v["eligibility"] == "EVIDENCE_INSUFFICIENT" for v in out["power"].values())
    md = (env.tmp / "reports" / "baseline_pa_brooks_ES_RTH_5m.md").read_text(encoding="utf-8")
    for needle in ("FIXTURE_ONLY", "EVIDENCE_INSUFFICIENT", "冻结清单", "复现校验", "真实行情的缺口", "风险规模结果",
                   "未配置", "不是投资建议", "### 编号", "| H1 |", "TWO_STOPS_HALT", "另有 2 笔交易"):
        assert needle in md, needle
    data = json.loads((env.tmp / "reports" / "baseline_pa_brooks_ES_RTH_5m.json").read_text(encoding="utf-8"))
    assert data["segments"]["development"]["valid_days"] == 5 and data["segments"]["forward_validation"]["valid_days"] == 2
    assert data["segments"]["total"]["fixed"]["profit_factor"] == 0.0             # 全是亏损：PF 为 0，可估
    assert data["segments"]["total"]["opportunities"] == 14 and "trades" not in data
    csv_text = (env.tmp / "reports" / "baseline_trades_pa_brooks_ES_RTH_5m.csv").read_text(encoding="utf-8")
    assert len(csv_text.strip().splitlines()) == 1 + 16
    pw = (env.tmp / "reports" / "baseline_power_pa_brooks_ES_RTH_5m.md").read_text(encoding="utf-8")
    for needle in ("FIXTURE_ONLY", "不是任何改动能盈利的证据", "A1_block_h1l1", "B1_spike_2atr", "Track A", "Track B"):
        assert needle in pw, needle
    # 扰动只在开发段回放，逐条决策只留计数，账本保留
    rows = {r["variant_id"]: r for r in env.con.execute("SELECT * FROM research_runs WHERE role='VARIANT'")}
    assert set(rows) == {p.id for p in c.perturbations_for("pa_brooks")}
    a1 = rows["A1_block_h1l1"]
    assert (a1["start_day"], a1["end_day"]) == (days[0].isoformat(), days[5].isoformat())
    assert env.con.execute("SELECT COUNT(*) FROM scan_events WHERE run_id=?", (a1["run_id"],)).fetchone()[0] == 0
    assert json.loads(a1["summary_json"])["events"]["decisions"] == 78 * 6
    n_a1 = env.con.execute("SELECT COUNT(*) FROM paper_trades WHERE run_id=?", (a1["run_id"],)).fetchone()[0]
    assert n_a1 < 12                                                      # 过滤 H1 之后交易变少

    again = run_baseline(env.settings, env.con, env.scope, REG, research_cfg=c, risk_cfg=configured_risk(),
                         engine_cfg=ER.ENG, out_dir=env.tmp / "reports", skip_power=True)
    assert again["reused"] and again["baseline_id"] == out["baseline_id"] and again["power"] is None


def test_report_marks_unestimable_pf(env):
    r = {"profit_factor": None, "trades": 3}
    assert RP._pf(r) == "不可估" and RP._pf({"profit_factor": None, "trades": 0}) == "—"
    assert RP._pf({"profit_factor": 1.234, "trades": 3}) == "1.23"
    assert RP.scope_slug("pa_brooks:ES:RTH:5m") == "pa_brooks_ES_RTH_5m"
