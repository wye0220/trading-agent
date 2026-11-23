"""策略插件接口（SPI）。所有策略共用；这里不出现任何具体策略的字段。

策略是无副作用的纯函数：不读写文件、数据库或网络，不修改传入的上下文和状态。
价格一律是整数 tick。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

SIGNAL_STATUSES = ("WAIT", "SETUP_VALID", "DATA_ERROR")
SIDES = ("LONG", "SHORT")
ORDER_TYPES = ("STOP_LIMIT_NEXT_BAR", "MARKET_NEXT_OPEN", "LIMIT_RETRACE")
TARGET_KINDS = ("ABSOLUTE_PRICE_TICKS", "OFFSET_FROM_FILL_TICKS", "INITIAL_RISK_MULTIPLE")
ALIGNMENTS = ("ALIGNED", "DEVIATES", "UNKNOWN")
EXIT_REASONS = ("STOP", "TARGET", "FORCED_FLAT", "REVERSAL", "OTHER")
FIXED_SCALE = 1_000_000_000          # 指标的定点精度：1e-9 tick


class ContractViolation(ValueError):
    """违反接口约定的输入或输出。"""


def _frozen_map(m: Mapping | None) -> Mapping:
    return MappingProxyType(dict(m or {}))


@dataclass(frozen=True, slots=True)
class Bar:
    """一根已收线的 K 线。价格为整数 tick，时刻为 UTC epoch 秒。"""
    start_utc: int
    end_utc: int
    open: int
    high: int
    low: int
    close: int
    volume: int = 0
    session_type: str = "RTH"
    trading_day: str = ""


@dataclass(frozen=True, slots=True)
class Swing:
    kind: str                # HIGH | LOW
    price: int
    bar_start_utc: int
    known_at_bar_start_utc: int     # 右侧第 2 根的起始时刻：它收线后该 swing 才可用


@dataclass(frozen=True, slots=True)
class PriceRange:
    high: int
    low: int


@dataclass(frozen=True, slots=True)
class Adr:
    """平均日波幅。value_ticks 是有效日高低差的均值（分数），valid_days 是参与平均的天数。"""
    value_ticks: Fraction
    valid_days: int
    days: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """本交易日已完结的一笔交易（由引擎提供，只读）。net_sign：净盈亏的符号，1、0 或 -1。"""
    setup_id: str
    exit_reason: str
    net_sign: int

    def __post_init__(self):
        if self.exit_reason not in EXIT_REASONS:
            raise ContractViolation(f"未知的退出原因：{self.exit_reason!r}")
        if self.net_sign not in (-1, 0, 1):
            raise ContractViolation(f"net_sign 只能是 -1、0、1：{self.net_sign!r}")


@dataclass(frozen=True, slots=True)
class FeatureRequirements:
    features: frozenset[str]
    warmup_bars: int = 0


@dataclass(frozen=True, slots=True)
class FeatureContext:
    """策略每根 K 线收到的只读输入。只含数据，没有数据库连接或回调。

    bars 按时间升序，最后一根是刚收线的当前 K（同一实际合约，含 ETH 背景）。
    ema20、atr14 是定点整数，单位 1e-9 tick（见 FIXED_SCALE）；不可用时为 None。
    """
    scope: str
    instrument_root: str
    contract_id: str
    as_of_utc: int
    bars: tuple[Bar, ...]
    rth_open_utc: int
    rth_close_utc: int
    ema20: int | None = None
    atr14: int | None = None
    ema_side_run: int = 0            # 连续收在 EMA 同侧的根数：正数在上方，负数在下方，0 为不可用或正好在线上
    swings: tuple[Swing, ...] = ()
    rolling_20_range: PriceRange | None = None
    intraday_rth_range: PriceRange | None = None
    adr10: Adr | None = None
    htf_1h: tuple[Bar, ...] = ()
    reference_levels: Mapping[str, Any] = field(default_factory=dict)
    closed_trades: tuple[ClosedTrade, ...] = ()
    known_at_utc: int | None = None

    def __post_init__(self):
        if not self.bars:
            raise ContractViolation("FeatureContext.bars 不能为空")
        object.__setattr__(self, "reference_levels", _frozen_map(self.reference_levels))

    @property
    def bar(self) -> Bar:
        return self.bars[-1]

    @property
    def prev_bar(self) -> Bar | None:
        return self.bars[-2] if len(self.bars) > 1 else None


class StrategyState:
    """策略私有状态：结构版本号 + 只含 JSON 基本类型的数据。不可变；data 每次返回一份副本。"""

    __slots__ = ("_version", "_json")

    def __init__(self, state_schema_version: int, data: Mapping[str, Any] | None = None):
        if isinstance(state_schema_version, bool) or not isinstance(state_schema_version, int):
            raise ContractViolation("state_schema_version 必须是整数")
        try:
            text = json.dumps(dict(data or {}), sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            raise ContractViolation(f"策略状态只能含 JSON 基本类型：{e}") from None
        object.__setattr__(self, "_version", state_schema_version)
        object.__setattr__(self, "_json", text)

    def __setattr__(self, *a):
        raise AttributeError("StrategyState 不可修改")

    __delattr__ = __setattr__

    @property
    def state_schema_version(self) -> int:
        return self._version

    @property
    def data(self) -> dict:
        return json.loads(self._json)

    def to_json(self) -> str:
        return json.dumps({"state_schema_version": self._version, "data": json.loads(self._json)},
                          sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "StrategyState":
        try:
            obj = json.loads(text)
            return cls(obj["state_schema_version"], obj["data"])
        except (ValueError, KeyError, TypeError) as e:
            raise ContractViolation(f"无法解析策略状态：{e}") from None

    def __eq__(self, other):
        return isinstance(other, StrategyState) and (self._version, self._json) == (other._version, other._json)

    def __hash__(self):
        return hash((self._version, self._json))

    def __repr__(self):
        return f"StrategyState(v{self._version}, {self._json})"


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """目标的三种通用描述之一。引擎在成交后据此定价。"""
    kind: str
    value: int | Fraction

    def __post_init__(self):
        if self.kind not in TARGET_KINDS:
            raise ContractViolation(f"目标类型只能是 {TARGET_KINDS}：{self.kind!r}")
        v = self.value
        if isinstance(v, bool) or not isinstance(v, (int, Fraction)):
            raise ContractViolation(f"目标数值必须是整数或分数：{v!r}")
        if self.kind != "INITIAL_RISK_MULTIPLE" and not isinstance(v, int):
            raise ContractViolation(f"{self.kind} 的数值必须是整数 tick：{v!r}")
        if v <= 0:
            raise ContractViolation(f"目标数值必须为正：{v!r}")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value if isinstance(self.value, int) else str(self.value)}


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """标准订单意图。没有可执行代码，也没有靠策略名决定成交方式的字段。"""
    side: str
    order_type: str
    stop_ticks: int
    target: TargetSpec
    setup_id: str
    trigger_ticks: int | None = None
    limit_ticks: int | None = None
    ttl_bars: int = 1
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.side not in SIDES:
            raise ContractViolation(f"方向只能是 {SIDES}：{self.side!r}")
        if self.order_type not in ORDER_TYPES:
            raise ContractViolation(f"订单类型只能是 {ORDER_TYPES}：{self.order_type!r}")
        if not isinstance(self.target, TargetSpec):
            raise ContractViolation("target 必须是 TargetSpec")
        if not self.setup_id:
            raise ContractViolation("setup_id 不能为空")
        if isinstance(self.ttl_bars, bool) or not isinstance(self.ttl_bars, int) or self.ttl_bars < 1:
            raise ContractViolation(f"ttl_bars 必须是正整数：{self.ttl_bars!r}")
        for name in ("stop_ticks", "trigger_ticks", "limit_ticks"):
            v = getattr(self, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise ContractViolation(f"{name} 必须是整数 tick：{v!r}")
        sign = 1 if self.side == "LONG" else -1
        if self.order_type == "STOP_LIMIT_NEXT_BAR":
            if self.trigger_ticks is None or self.limit_ticks is None:
                raise ContractViolation("STOP_LIMIT_NEXT_BAR 需要触发价和限价")
            if sign * (self.limit_ticks - self.trigger_ticks) < 0:
                raise ContractViolation("限价在触发价的错误一侧：做多时限价不得低于触发价，做空时不得高于")
        if self.order_type == "LIMIT_RETRACE" and self.limit_ticks is None:
            raise ContractViolation("LIMIT_RETRACE 需要限价")
        entry = self.trigger_ticks if self.trigger_ticks is not None else self.limit_ticks
        if entry is not None and sign * (entry - self.stop_ticks) <= 0:
            raise ContractViolation("保护止损在入场的错误一侧：做多时止损必须低于入场，做空时必须高于")
        if self.target.kind == "ABSOLUTE_PRICE_TICKS" and entry is not None and sign * (self.target.value - entry) <= 0:
            raise ContractViolation("绝对目标价在入场的错误一侧")
        object.__setattr__(self, "evidence", _frozen_map(self.evidence))

    def to_dict(self) -> dict:
        return {"side": self.side, "order_type": self.order_type, "trigger_ticks": self.trigger_ticks,
                "limit_ticks": self.limit_ticks, "ttl_bars": self.ttl_bars, "stop_ticks": self.stop_ticks,
                "target": self.target.to_dict(), "setup_id": self.setup_id, "evidence": dict(self.evidence)}


@dataclass(frozen=True, slots=True)
class DecisionMetadata:
    """一次决策的结构化说明。通用字段在这里；策略专有的结构放在 structure 和 evidence 里。"""
    strategy_family: str
    strategy_version: str
    contract_id: str
    signal_bar_end_utc: int
    known_at_utc: int | None
    signal_status: str
    side: str | None = None
    setup: str | None = None
    setup_id: str | None = None
    label: str | None = None
    skip_reason: str | None = None
    valid_until_utc: int | None = None
    entry_trigger_ticks: int | None = None
    entry_limit_ticks: int | None = None
    stop_ticks: int | None = None
    target: Mapping[str, Any] | None = None
    target_rule: str | None = None
    next_confirmation: str | None = None
    structure: Mapping[str, Any] = field(default_factory=dict)     # 周期、上一级方向等结构状态
    evidence: Mapping[str, Any] = field(default_factory=dict)      # 信号 K 质量、过滤器结论、依据的数值

    def __post_init__(self):
        if self.signal_status not in SIGNAL_STATUSES:
            raise ContractViolation(f"signal_status 只能是 {SIGNAL_STATUSES}：{self.signal_status!r}")
        object.__setattr__(self, "structure", _frozen_map(self.structure))
        object.__setattr__(self, "evidence", _frozen_map(self.evidence))
        if self.target is not None:
            object.__setattr__(self, "target", _frozen_map(self.target))

    def to_dict(self) -> dict:
        out = {}
        for name in self.__dataclass_fields__:
            v = getattr(self, name)
            out[name] = dict(v) if isinstance(v, Mapping) else v
        return out


@dataclass(frozen=True, slots=True)
class StrategyStepResult:
    signal_status: str
    order_intent: OrderIntent | None
    decision_metadata: DecisionMetadata
    next_state: StrategyState

    def __post_init__(self):
        if self.signal_status not in SIGNAL_STATUSES:
            raise ContractViolation(f"signal_status 只能是 {SIGNAL_STATUSES}：{self.signal_status!r}")
        if self.signal_status != "SETUP_VALID" and self.order_intent is not None:
            raise ContractViolation("只有 SETUP_VALID 才能带订单意图")
        if self.signal_status == "SETUP_VALID" and self.order_intent is None:
            raise ContractViolation("SETUP_VALID 必须带订单意图")
        if self.decision_metadata.signal_status != self.signal_status:
            raise ContractViolation("决策元数据的 signal_status 与结果不一致")
        if not isinstance(self.next_state, StrategyState):
            raise ContractViolation("next_state 必须是 StrategyState")


@dataclass(frozen=True, slots=True)
class TraderPlan:
    """人工交易计划（供一致性评估）。"""
    side: str
    entry_ticks: int
    stop_ticks: int | None = None
    target_ticks: int | None = None
    quantity: int = 1
    contract_id: str = ""
    reason: str = ""

    def __post_init__(self):
        if self.side not in SIDES:
            raise ContractViolation(f"方向只能是 {SIDES}：{self.side!r}")


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    alignment: str
    flags: tuple[str, ...] = ()
    setup_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.alignment not in ALIGNMENTS:
            raise ContractViolation(f"alignment 只能是 {ALIGNMENTS}：{self.alignment!r}")
        object.__setattr__(self, "flags", tuple(self.flags))
        object.__setattr__(self, "evidence", _frozen_map(self.evidence))


@runtime_checkable
class StrategyProvider(Protocol):
    """所有策略都要实现的接口。"""

    strategy_family: str
    version: str

    def feature_requirements(self) -> FeatureRequirements: ...

    def initial_state(self) -> StrategyState: ...

    def on_bar(self, ctx: FeatureContext, prev_state: StrategyState) -> StrategyStepResult: ...

    def evaluate_pretrade_alignment(self, ctx: FeatureContext, state: StrategyState,
                                    plan: TraderPlan) -> AlignmentResult: ...
