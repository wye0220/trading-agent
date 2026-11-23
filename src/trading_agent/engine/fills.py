"""通用成交模型。纯函数：给定订单簿（挂单 + 仓位）和一根子 K，返回新的订单簿和事件。

规则见 docs/engine.md。改动这里的成交规则或事件顺序必须同时改 ENGINE_VERSION。

内部用"定向"坐标处理多空：做空时把价格取负并交换高低，套用同一套做多的逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction

from ..strategies.base import OrderIntent, TargetSpec

ENGINE_VERSION = "fill-v1.0"

WORKING = ("ARMED", "TRIGGERED")
ORDER_STATUSES = ("ARMED", "TRIGGERED", "FILLED", "EXPIRED", "CANCELLED")
EXIT_REASONS = ("STOP", "TARGET", "FORCED_FLAT", "REVERSAL")


@dataclass(frozen=True, slots=True)
class SubBar:
    """喂给成交模型的一根行情：有 1 分钟线时是 1 分钟线，否则是 5 分钟线本身。价格为整数 tick。"""
    start_utc: int
    end_utc: int
    open: int
    high: int
    low: int
    close: int


@dataclass(frozen=True, slots=True)
class FillConfig:
    entry_slippage_ticks: int = 0
    stop_slippage_ticks: int = 0
    # 跨着激活时刻的子 K 能否用于 MARKET_NEXT_OPEN：只有当子 K 就是信号周期本身（没有更细的行情）时为真
    market_fill_on_straddle: bool = False


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    order_id: str
    setup_id: str
    side: str                      # LONG | SHORT
    order_type: str
    stop_ticks: int
    target: TargetSpec
    armed_at_utc: int
    expires_at_utc: int            # 有效期截止（已与截止强平时刻取较早者）
    flatten_at_utc: int            # 所属交易日的截止强平时刻
    trigger_ticks: int | None = None
    limit_ticks: int | None = None
    status: str = "ARMED"
    bars_seen: int = 0
    flags: tuple[str, ...] = ()
    end_reason: str | None = None

    @classmethod
    def from_intent(cls, intent: OrderIntent, order_id: str, armed_at_utc: int, expires_at_utc: int,
                    flatten_at_utc: int) -> "WorkingOrder":
        return cls(order_id=order_id, setup_id=intent.setup_id, side=intent.side, order_type=intent.order_type,
                   stop_ticks=intent.stop_ticks, target=intent.target, armed_at_utc=armed_at_utc,
                   expires_at_utc=min(expires_at_utc, flatten_at_utc), flatten_at_utc=flatten_at_utc,
                   trigger_ticks=intent.trigger_ticks, limit_ticks=intent.limit_ticks)


@dataclass(frozen=True, slots=True)
class Position:
    position_id: str
    order_id: str
    setup_id: str
    side: str
    entry_ticks: int
    entry_time_utc: int
    initial_stop_ticks: int
    stop_ticks: int
    target_ticks: int
    flatten_at_utc: int
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Event:
    """成交模型产生的一个事件。kind：TRIGGERED | ENTRY | EXIT | EXPIRED | CANCELLED | NOTE。"""
    kind: str
    time_utc: int
    order_id: str | None = None
    position_id: str | None = None
    price_ticks: int | None = None
    reason: str | None = None
    flags: tuple[str, ...] = ()
    position: Position | None = None       # ENTRY：新仓位；EXIT：被平掉的仓位


@dataclass(frozen=True, slots=True)
class Book:
    orders: tuple[WorkingOrder, ...] = ()
    position: Position | None = None


def _d(side: str) -> int:
    return 1 if side == "LONG" else -1


def _ob(bar: SubBar, d: int) -> tuple[int, int, int, int]:
    return (bar.open, bar.high, bar.low, bar.close) if d > 0 else (-bar.open, -bar.low, -bar.high, -bar.close)


def _add(flags: tuple[str, ...], *new: str) -> tuple[str, ...]:
    out = list(flags)
    for f in new:
        if f and f not in out:
            out.append(f)
    return tuple(out)


def price_target(target: TargetSpec, side: str, fill_ticks: int, stop_ticks: int) -> int:
    """成交后把目标描述换成价格。风险倍数向不利方向（离入场更远）取整到整数 tick。"""
    d = _d(side)
    if target.kind == "ABSOLUTE_PRICE_TICKS":
        return int(target.value)
    if target.kind == "OFFSET_FROM_FILL_TICKS":
        return fill_ticks + d * int(target.value)
    dist = Fraction(d * (fill_ticks - stop_ticks)) * Fraction(target.value)
    return fill_ticks + d * (-(-dist.numerator // dist.denominator))


def _entry_fill(order: WorkingOrder, bar: SubBar, cfg: FillConfig, straddle: bool):
    """订单在这根子 K 上的入场判定。返回 (新状态, 成交价(定向) 或 None, 成交方式, 是否刚触发)。

    成交方式：open（开盘价成交）、intrabar（从下方盘中触发）、gap_return（越过限价后回落到限价）、limit（回落限价单）。
    """
    d = _d(order.side)
    o, h, l, c = _ob(bar, d)
    if order.order_type == "STOP_LIMIT_NEXT_BAR":
        P, L = d * order.trigger_ticks, d * order.limit_ticks
        if order.status == "ARMED":
            if o >= P:
                if o <= L:
                    return "FILLED", o, "open", True
                if l <= L:
                    return "FILLED", L, "gap_return", True
                return "TRIGGERED", None, None, True
            if h >= P:
                return "FILLED", min(P + cfg.entry_slippage_ticks, L), "intrabar", True
            return "ARMED", None, None, False
        if l <= L:                                   # 已触发，等价格回到限价
            return ("FILLED", o, "open", False) if o <= L else ("FILLED", L, "gap_return", False)
        return "TRIGGERED", None, None, False
    if order.order_type == "MARKET_NEXT_OPEN":
        if straddle and not cfg.market_fill_on_straddle:
            return "ARMED", None, None, False        # 等第一根完全在激活之后的子 K
        return "FILLED", o + cfg.entry_slippage_ticks, "open", False
    if order.order_type == "LIMIT_RETRACE":
        L = d * order.limit_ticks
        if l <= L:
            return "FILLED", L, "limit", False       # 不给价格改善
        return "ARMED", None, None, False
    raise ValueError(f"未知的订单类型：{order.order_type}")


def _exit(pos: Position, time_utc: int, price: int, reason: str, *flags: str) -> Event:
    closed = replace(pos, flags=_add(pos.flags, *flags))
    return Event("EXIT", time_utc, pos.order_id, pos.position_id, price, reason, closed.flags, closed)


def _manage(pos: Position, bar: SubBar, cfg: FillConfig, *, at_open: bool = True) -> tuple[Position | None, list[Event]]:
    """已有仓位（不是这根子 K 上开的）在这根子 K 上的止损与目标。"""
    d = _d(pos.side)
    o, h, l, c = _ob(bar, d)
    S, T = d * pos.stop_ticks, d * pos.target_ticks
    slip = cfg.stop_slippage_ticks
    if at_open:
        if o < S:                                    # 跳空越过止损
            return None, [_exit(pos, bar.start_utc, d * (o - slip), "STOP", "GAP_THROUGH_STOP")]
        if o >= T:                                   # 跳空越过目标：保守按目标价成交
            return None, [_exit(pos, bar.start_utc, d * T, "TARGET")]
    stop_hit, target_hit = l <= S, h >= T
    if stop_hit and target_hit:
        return None, [_exit(pos, bar.end_utc, d * (S - slip), "STOP", "AMBIGUOUS_BAR")]
    if stop_hit:
        return None, [_exit(pos, bar.end_utc, d * (S - slip), "STOP")]
    if target_hit:
        return None, [_exit(pos, bar.end_utc, d * T, "TARGET")]
    return pos, []


def apply_bar(book: Book, bar: SubBar, cfg: FillConfig = FillConfig()) -> tuple[Book, list[Event]]:
    """用一根子 K 推进订单簿。子 K 必须按时间顺序喂入，且在调用时已经可见。

    顺序：截止强平 → 旧仓位的开盘跳空 → 挂单（含反手） → 旧仓位的盘中止损与目标。
    """
    events: list[Event] = []
    pos = book.position
    orders = list(book.orders)

    # 1) 截止强平：这根子 K 起始时刻不早于仓位所属交易日的截止时刻
    if pos is not None and bar.start_utc >= pos.flatten_at_utc:
        d = _d(pos.side)
        events.append(_exit(pos, bar.start_utc, bar.open - d * cfg.stop_slippage_ticks, "FORCED_FLAT"))
        pos = None
    kept = []
    for od in orders:
        if od.status in WORKING and bar.start_utc >= od.flatten_at_utc:
            events.append(Event("CANCELLED", bar.start_utc, od.order_id, reason="CUTOFF", flags=od.flags))
            kept.append(replace(od, status="CANCELLED", end_reason="CUTOFF"))
        else:
            kept.append(od)
    orders = kept

    # 2) 旧仓位：开盘跳空
    old = pos
    gap_done = False
    if old is not None:
        d = _d(old.side)
        o = _ob(bar, d)[0]
        if o < d * old.stop_ticks or o >= d * old.target_ticks:
            pos, ev = _manage(old, bar, cfg, at_open=True)
            events += ev
            gap_done = True
            old = None

    # 3) 挂单
    reversed_now = False
    out_orders = []
    for od in orders:
        if od.status not in WORKING:
            out_orders.append(od)
            continue
        if bar.end_utc <= od.armed_at_utc:           # 完全在激活之前：对这张订单不存在
            out_orders.append(od)
            continue
        if bar.start_utc >= od.expires_at_utc:
            flags = _add(od.flags, "DATA_GAP_IN_WINDOW") if od.bars_seen == 0 else od.flags
            events.append(Event("EXPIRED", od.expires_at_utc, od.order_id, reason="TTL", flags=flags))
            out_orders.append(replace(od, status="EXPIRED", flags=flags, end_reason="TTL"))
            continue
        straddle = bar.start_utc < od.armed_at_utc < bar.end_utc
        status, fill, how, triggered = _entry_fill(od, bar, cfg, straddle)
        od = replace(od, bars_seen=od.bars_seen + 1)
        d = _d(od.side)
        if triggered and od.order_type == "STOP_LIMIT_NEXT_BAR":
            events.append(Event("TRIGGERED", bar.start_utc if how in ("open", None) else bar.end_utc, od.order_id,
                                price_ticks=od.trigger_ticks, flags=od.flags))
        if fill is None:
            out_orders.append(replace(od, status=status))
            continue

        flags = _add(od.flags, "AMBIGUOUS_ENTRY") if straddle else od.flags
        fill_px = d * fill
        t_fill = bar.start_utc if how == "open" else bar.end_utc
        cur = pos if pos is not None else None
        if cur is not None and cur.side == od.side:  # 同向已有仓位：不加仓
            events.append(Event("CANCELLED", t_fill, od.order_id, reason="POSITION_OPEN_SAME_SIDE", flags=flags))
            out_orders.append(replace(od, status="CANCELLED", flags=flags, end_reason="POSITION_OPEN_SAME_SIDE"))
            continue
        if cur is not None:                          # 反手：旧仓在同一模型价格平掉；它的止损若在这根触及则止损优先
            cd = _d(cur.side)
            co, ch, cl, cc = _ob(bar, cd)
            if cl <= cd * cur.stop_ticks:
                events.append(_exit(cur, bar.end_utc, cur.stop_ticks - cd * cfg.stop_slippage_ticks, "STOP",
                                    "AMBIGUOUS_BAR"))
            else:
                amb = ("AMBIGUOUS_PATH",) if ch >= cd * cur.target_ticks else ()
                events.append(_exit(cur, t_fill, fill_px, "REVERSAL", *amb))
            pos = None
            reversed_now = True

        S = d * od.stop_ticks
        new = Position(position_id="pos_" + od.order_id, order_id=od.order_id, setup_id=od.setup_id, side=od.side,
                       entry_ticks=fill_px, entry_time_utc=t_fill, initial_stop_ticks=od.stop_ticks,
                       stop_ticks=od.stop_ticks, target_ticks=0, flatten_at_utc=od.flatten_at_utc, flags=flags)
        invalid = fill <= S
        tgt = None if invalid else price_target(od.target, od.side, fill_px, od.stop_ticks)
        if tgt is not None and d * tgt <= fill:
            invalid = True
        new = replace(new, target_ticks=fill_px if invalid else tgt)
        events.append(Event("ENTRY", t_fill, od.order_id, new.position_id, fill_px, how, flags, new))
        out_orders.append(replace(od, status="FILLED", flags=flags, end_reason="FILLED"))

        if invalid:                                  # 成交价已越过止损，或目标不在顺方向一侧：立即离场
            events.append(_exit(new, bar.end_utc, fill_px, "STOP", "INVALID_AFTER_FILL"))
            continue
        # 入场当根的止损与目标
        o, h, l, c = _ob(bar, d)
        T = d * new.target_ticks
        stop_hit, target_hit = l <= S, h >= T
        slip = cfg.stop_slippage_ticks
        if stop_hit:
            amb = how != "open" or target_hit        # 开盘成交且只触及止损时先后是确定的
            events.append(_exit(new, bar.end_utc, d * (S - slip), "STOP", *(("AMBIGUOUS_BAR",) if amb else ())))
        elif target_hit and how in ("open", "intrabar"):
            events.append(_exit(new, bar.end_utc, d * T, "TARGET"))      # 高于入场价的价格必然出现在入场之后
        elif target_hit:
            pos = replace(new, flags=_add(new.flags, "AMBIGUOUS_PATH"))   # 回落成交：最高价可能在成交之前，不算
        else:
            pos = new

    # 4) 旧仓位：盘中止损与目标（没有跳空、没有被反手）
    if old is not None and not gap_done and not reversed_now and pos is old:
        pos, ev = _manage(old, bar, cfg, at_open=False)
        events += ev
    return Book(tuple(out_orders), pos), events


def expire_orders(book: Book, now_utc: int) -> tuple[Book, list[Event]]:
    """把已过有效期的挂单标为 EXPIRED。有效期内一根子 K 都没见到的带 DATA_GAP_IN_WINDOW。"""
    events, out = [], []
    for od in book.orders:
        if od.status in WORKING and now_utc >= od.expires_at_utc:
            flags = _add(od.flags, "DATA_GAP_IN_WINDOW") if od.bars_seen == 0 else od.flags
            reason = "CUTOFF" if od.expires_at_utc >= od.flatten_at_utc else "TTL"
            kind = "CANCELLED" if reason == "CUTOFF" else "EXPIRED"
            events.append(Event(kind, od.expires_at_utc, od.order_id, reason=reason, flags=flags))
            out.append(replace(od, status=kind, flags=flags, end_reason=reason))
        else:
            out.append(od)
    return Book(tuple(out), book.position), events


def flatten_check(book: Book, now_utc: int) -> tuple[Book, list[Event]]:
    """截止时刻已过而仓位还在（之后没有行情）：标为无法定价，等第一个有效价格由 apply_bar 平掉。"""
    pos = book.position
    if pos is not None and now_utc >= pos.flatten_at_utc and "UNPRICEABLE" not in pos.flags:
        pos = replace(pos, flags=_add(pos.flags, "UNPRICEABLE", "UNABLE_TO_FLATTEN"))
        return Book(book.orders, pos), [Event("NOTE", now_utc, pos.order_id, pos.position_id,
                                              reason="UNABLE_TO_FLATTEN", flags=pos.flags, position=pos)]
    return book, []
