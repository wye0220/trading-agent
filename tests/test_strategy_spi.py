import dataclasses
import re
from fractions import Fraction

import pytest

import helpers as H
from trading_agent.strategies import base as B
from trading_agent.strategies.registry import StrategyRegistry, default_registry

T2R = B.TargetSpec("INITIAL_RISK_MULTIPLE", 2)


def bar(i=0, o=20000, h=20004, l=19996, c=20002):
    return B.Bar(1000 + 300 * i, 1300 + 300 * i, o, h, l, c, 10, "RTH", "2025-11-25")


def ctx(n=3, **kw):
    return B.FeatureContext(scope="mock:ES:RTH:5m", instrument_root="ES", contract_id="ESZ2025", as_of_utc=9999,
                            bars=tuple(bar(i) for i in range(n)), rth_open_utc=0, rth_close_utc=10**10, **kw)


def meta(status="WAIT", **kw):
    return B.DecisionMetadata("mock", "v0", "ESZ2025", 1300, None, status, **kw)


# ---------------------------------------------------------------- 2.1 类型
def test_types_are_immutable():
    it = B.OrderIntent("LONG", "MARKET_NEXT_OPEN", 19990, T2R, "s1")
    for obj, attr in ((bar(), "open"), (T2R, "value"), (it, "side"), (ctx(), "atr14"), (meta(), "skip_reason"),
                      (B.TraderPlan("LONG", 1), "side"), (B.AlignmentResult("UNKNOWN"), "alignment")):
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            setattr(obj, attr, 1)
    with pytest.raises(TypeError):
        it.evidence["x"] = 1
    with pytest.raises(TypeError):
        ctx(reference_levels={"pdh": 1}).reference_levels["pdh"] = 2
    st = B.StrategyState(1, {"a": [1]})
    with pytest.raises(AttributeError):
        st.x = 1
    d = st.data
    d["a"].append(2)
    assert st.data == {"a": [1]}                       # 拿到的是副本，改它不影响状态


def test_step_result_consistency():
    st = B.StrategyState(1, {})
    it = B.OrderIntent("LONG", "MARKET_NEXT_OPEN", 19990, T2R, "s1")
    B.StrategyStepResult("WAIT", None, meta("WAIT"), st)
    B.StrategyStepResult("SETUP_VALID", it, meta("SETUP_VALID"), st)
    with pytest.raises(B.ContractViolation, match="只有 SETUP_VALID 才能带订单意图"):
        B.StrategyStepResult("WAIT", it, meta("WAIT"), st)
    with pytest.raises(B.ContractViolation, match="必须带订单意图"):
        B.StrategyStepResult("SETUP_VALID", None, meta("SETUP_VALID"), st)
    with pytest.raises(B.ContractViolation):
        B.StrategyStepResult("WAIT", None, meta("DATA_ERROR"), st)
    with pytest.raises(B.ContractViolation):
        meta("MAYBE")
    with pytest.raises(B.ContractViolation):
        B.FeatureContext("s", "ES", "ESZ2025", 1, (), 0, 1)


# ---------------------------------------------------------------- 2.2 订单意图
def test_valid_intents():
    a = B.OrderIntent("LONG", "STOP_LIMIT_NEXT_BAR", 19980, B.TargetSpec("OFFSET_FROM_FILL_TICKS", 81), "s",
                      trigger_ticks=20001, limit_ticks=20002)
    b = B.OrderIntent("SHORT", "STOP_LIMIT_NEXT_BAR", 20041, T2R, "s", trigger_ticks=19999, limit_ticks=19998)
    assert a.to_dict()["target"] == {"kind": "OFFSET_FROM_FILL_TICKS", "value": 81} and b.ttl_bars == 1
    B.OrderIntent("LONG", "LIMIT_RETRACE", 19980, B.TargetSpec("ABSOLUTE_PRICE_TICKS", 20040), "s", limit_ticks=20000)
    B.OrderIntent("LONG", "STOP_LIMIT_NEXT_BAR", 19980, B.TargetSpec("INITIAL_RISK_MULTIPLE", Fraction(3, 2)), "s",
                  trigger_ticks=20001, limit_ticks=20001)


@pytest.mark.parametrize("kw,needle", [
    (dict(side="LONG", stop_ticks=20001, trigger_ticks=20001, limit_ticks=20002), "保护止损在入场的错误一侧"),
    (dict(side="SHORT", stop_ticks=19990, trigger_ticks=19999, limit_ticks=19998), "保护止损在入场的错误一侧"),
    (dict(side="LONG", stop_ticks=19980, trigger_ticks=20001, limit_ticks=20000), "限价在触发价的错误一侧"),
    (dict(side="SHORT", stop_ticks=20041, trigger_ticks=19999, limit_ticks=20000), "限价在触发价的错误一侧"),
    (dict(side="LONG", stop_ticks=19980, trigger_ticks=20001), "需要触发价和限价"),
    (dict(side="BUY", stop_ticks=19980, trigger_ticks=20001, limit_ticks=20002), "方向只能是"),
    (dict(side="LONG", stop_ticks=19980, trigger_ticks=20001, limit_ticks=20002, ttl_bars=0), "ttl_bars"),
    (dict(side="LONG", stop_ticks=19980.5, trigger_ticks=20001, limit_ticks=20002), "必须是整数 tick"),
    (dict(side="LONG", stop_ticks=19980, trigger_ticks=20001, limit_ticks=20002, setup_id=""), "setup_id"),
])
def test_invalid_intents(kw, needle):
    kw.setdefault("setup_id", "s")
    with pytest.raises(B.ContractViolation, match=needle):
        B.OrderIntent(order_type="STOP_LIMIT_NEXT_BAR", target=T2R, **kw)


def test_invalid_order_type_and_targets():
    with pytest.raises(B.ContractViolation, match="订单类型只能是"):
        B.OrderIntent("LONG", "MARKET_NOW", 1, T2R, "s")
    with pytest.raises(B.ContractViolation, match="目标类型只能是"):
        B.TargetSpec("ADR_FRACTION", 1)
    for kind, v in (("OFFSET_FROM_FILL_TICKS", 0), ("INITIAL_RISK_MULTIPLE", -1), ("ABSOLUTE_PRICE_TICKS", 0),
                    ("OFFSET_FROM_FILL_TICKS", Fraction(1, 2)), ("OFFSET_FROM_FILL_TICKS", 1.5),
                    ("INITIAL_RISK_MULTIPLE", True)):
        with pytest.raises(B.ContractViolation):
            B.TargetSpec(kind, v)
    with pytest.raises(B.ContractViolation, match="绝对目标价在入场的错误一侧"):
        B.OrderIntent("LONG", "STOP_LIMIT_NEXT_BAR", 19980, B.TargetSpec("ABSOLUTE_PRICE_TICKS", 20000), "s",
                      trigger_ticks=20001, limit_ticks=20002)
    assert set(f.name for f in dataclasses.fields(B.OrderIntent)) == {
        "side", "order_type", "stop_ticks", "target", "setup_id", "trigger_ticks", "limit_ticks", "ttl_bars", "evidence"}


# ---------------------------------------------------------------- 2.3 状态
def test_state_roundtrip_and_validation():
    st = B.StrategyState(3, {"spike": {"d": 1, "legs": [1, 2]}, "day": None, "ok": True, "x": 1.5})
    again = B.StrategyState.from_json(st.to_json())
    assert again == st and again.state_schema_version == 3 and again.data == st.data
    assert B.StrategyState(1, {"b": 1, "a": 2}).to_json() == B.StrategyState(1, {"a": 2, "b": 1}).to_json()
    for bad in ({"x": {1, 2}}, {"x": object()}, {"x": float("nan")}, {"x": Fraction(1, 2)}):
        with pytest.raises(B.ContractViolation, match="JSON 基本类型"):
            B.StrategyState(1, bad)
    with pytest.raises(B.ContractViolation):
        B.StrategyState("1", {})
    with pytest.raises(B.ContractViolation):
        B.StrategyState.from_json("{not json")


# ---------------------------------------------------------------- 2.4 / 2.5 注册表与模拟策略
class MockStrategy:
    """与 PA 无关的策略：每根都发 MARKET_NEXT_OPEN + 2R。只用接口里的通用类型。"""
    strategy_family = "mock"
    version = "v0"

    def feature_requirements(self):
        return B.FeatureRequirements(frozenset(), 0)

    def initial_state(self):
        return B.StrategyState(1, {"n": 0})

    def on_bar(self, ctx, prev_state):
        n = prev_state.data["n"] + 1
        b = ctx.bar
        sid = f"mock-{b.end_utc}"
        intent = B.OrderIntent("LONG", "MARKET_NEXT_OPEN", b.low - 1, T2R, sid, evidence={"n": n})
        m = B.DecisionMetadata(self.strategy_family, self.version, ctx.contract_id, b.end_utc, ctx.known_at_utc,
                               "SETUP_VALID", side="LONG", setup="every_bar", setup_id=sid, stop_ticks=b.low - 1,
                               target=T2R.to_dict())
        return B.StrategyStepResult("SETUP_VALID", intent, m, B.StrategyState(1, {"n": n}))

    def evaluate_pretrade_alignment(self, ctx, state, plan):
        return B.AlignmentResult("UNKNOWN")


def test_registry():
    reg = StrategyRegistry()
    m = reg.register(MockStrategy())
    assert reg.get("mock") is m and reg.families() == ["mock"] and "mock" in reg
    with pytest.raises(B.ContractViolation, match="已注册"):
        reg.register(MockStrategy())
    with pytest.raises(B.ContractViolation, match=r"策略家族未注册：orb（已注册：mock）"):
        reg.get("orb")
    with pytest.raises(B.ContractViolation, match="没有实现 StrategyProvider"):
        reg.register(object())


def test_mock_strategy_runs_through_generic_interface_only():
    reg = StrategyRegistry()
    reg.register(MockStrategy())
    strat = reg.get("mock")
    assert isinstance(strat, B.StrategyProvider)
    state = strat.initial_state()
    seen = []
    for n in range(1, 5):
        res = strat.on_bar(ctx(n), state)
        state = B.StrategyState.from_json(res.next_state.to_json())
        it = res.order_intent
        seen.append((res.signal_status, it.order_type, it.target.kind, it.target.value, it.trigger_ticks))
        assert res.decision_metadata.to_dict()["setup_id"] == it.setup_id
    assert seen == [("SETUP_VALID", "MARKET_NEXT_OPEN", "INITIAL_RISK_MULTIPLE", 2, None)] * 4
    assert state.data == {"n": 4}
    assert strat.evaluate_pretrade_alignment(ctx(), state, B.TraderPlan("LONG", 20000)).alignment == "UNKNOWN"


def test_interface_has_no_strategy_specific_terms():
    src = H.PROJECT / "src" / "trading_agent" / "strategies"
    base = (src / "base.py").read_text(encoding="utf-8")
    assert not re.search(r"pa_brooks|spike|pullback|\bH1\b|\bL1\b|brooks", base, re.I)
    reg = (src / "registry.py").read_text(encoding="utf-8")
    assert not re.search(r"spike|pullback|\bH1\b", reg, re.I)
    # 注册表只在"注册内置策略"这一处提到具体策略
    assert len(re.findall(r"pa_brooks|PaBrooks", reg)) == 3


def test_default_registry_has_pa():
    p = default_registry().get("pa_brooks")
    assert p.strategy_family == "pa_brooks" and p.version == "v1.0" and isinstance(p, B.StrategyProvider)
    req = p.feature_requirements().features
    assert {"ema20", "atr14", "adr10", "htf_1h", "rolling_20_range", "intraday_rth_range"} <= req


# ---------------------------------------------------------------- 8.3 纯函数性（静态）
def test_strategy_and_feature_modules_do_no_io():
    src = H.PROJECT / "src" / "trading_agent"
    files = sorted((src / "strategies").glob("*.py")) + [src / "features.py"]
    assert len(files) >= 5
    banned = re.compile(
        r"^\s*(import|from)\s+(sqlite3|requests|urllib|http|socket|ssl|httpx|aiohttp|subprocess|shutil|webull|databento_dbn)\b"
        r"|open_runtime|open_release|\.execute\(|write_text|write_bytes|open\([^)]*[\"'](w|a|wb|ab|r\+)[\"']"
        r"|context\.visible_bars|os\.remove|\.unlink\(", re.M)
    for f in files:
        assert not banned.search(f.read_text(encoding="utf-8")), f
    pa = (src / "strategies" / "pa_brooks.py").read_text(encoding="utf-8")
    assert pa.count("open(") == 1                      # 只有 load_config 读配置文件
    assert "open(" not in pa.split("class PaBrooksStrategy")[1].split("def render_zh")[0]
