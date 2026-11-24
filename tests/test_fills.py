import dataclasses
from fractions import Fraction

import pytest

import fill_runner as R
from trading_agent.engine import fills as F
from trading_agent.strategies.base import OrderIntent, TargetSpec

VECTORS = R.all_vectors()


def test_vector_inventory():
    assert len(VECTORS) >= 32


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_fill_vector_forward(path):
    R.run(R.load(path))


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_fill_vector_mirrored(path):
    R.run(R.load(path), mirror=True)


def test_mirror_is_a_true_mirror():
    v = R.load(R.VECTOR_DIR / "atomic_reversal.yaml")
    (b1, e1), (b2, e2) = R.run(v), R.run(v, mirror=True)
    axis = R.ticks(5000)
    assert [e.kind for e in e1] == [e.kind for e in e2] and [e.reason for e in e1] == [e.reason for e in e2]
    for a, b in zip(e1, e2):
        if a.price_ticks is not None:
            assert b.price_ticks == 2 * axis - a.price_ticks
    assert (b1.position.side, b2.position.side) == ("SHORT", "LONG")
    assert b2.position.target_ticks == 2 * axis - b1.position.target_ticks


def test_runner_reports_mismatch():
    v = R.load(R.VECTOR_DIR / "target_hit.yaml")
    v["expect"]["events"][0]["reason"] = "STOP"
    with pytest.raises(AssertionError, match=r"target_hit\.yaml 第 0 个事件 \[reason\]：期望 'STOP'，得到 'TARGET'"):
        R.run(v)


# ---------------------------------------------------------------- 类型与版本
def test_types_immutable_and_versioned():
    assert F.ENGINE_VERSION == "fill-v1.0"
    od = F.WorkingOrder("o", "s", "LONG", "MARKET_NEXT_OPEN", 100, TargetSpec("INITIAL_RISK_MULTIPLE", 2), 0, 10, 20)
    for obj, attr in ((F.SubBar(0, 60, 1, 2, 0, 1), "open"), (od, "status"), (F.Book(), "position"),
                      (F.FillConfig(), "stop_slippage_ticks"), (F.Event("NOTE", 0), "kind")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, 1)
    assert F.ORDER_STATUSES == ("ARMED", "TRIGGERED", "FILLED", "EXPIRED", "CANCELLED")


def test_from_intent_caps_expiry_at_cutoff():
    it = OrderIntent("LONG", "STOP_LIMIT_NEXT_BAR", 90, TargetSpec("OFFSET_FROM_FILL_TICKS", 8), "sid",
                     trigger_ticks=100, limit_ticks=101)
    od = F.WorkingOrder.from_intent(it, "o1", armed_at_utc=1000, expires_at_utc=2000, flatten_at_utc=1500)
    assert (od.expires_at_utc, od.flatten_at_utc, od.status, od.setup_id) == (1500, 1500, "ARMED", "sid")
    assert (od.trigger_ticks, od.limit_ticks, od.stop_ticks) == (100, 101, 90)


# ---------------------------------------------------------------- 目标定价
@pytest.mark.parametrize("target,side,fill,stop,want", [
    (TargetSpec("ABSOLUTE_PRICE_TICKS", 20100), "LONG", 20000, 19984, 20100),
    (TargetSpec("OFFSET_FROM_FILL_TICKS", 81), "LONG", 20001, 19987, 20082),
    (TargetSpec("OFFSET_FROM_FILL_TICKS", 81), "SHORT", 20001, 20015, 19920),
    (TargetSpec("INITIAL_RISK_MULTIPLE", 2), "LONG", 20000, 19984, 20032),
    (TargetSpec("INITIAL_RISK_MULTIPLE", 2), "SHORT", 20000, 20016, 19968),
    (TargetSpec("INITIAL_RISK_MULTIPLE", Fraction(3, 2)), "LONG", 20000, 19985, 20023),    # 15×1.5=22.5 → 向上取整
    (TargetSpec("INITIAL_RISK_MULTIPLE", Fraction(3, 2)), "SHORT", 20000, 20015, 19977),
])
def test_price_target(target, side, fill, stop, want):
    assert F.price_target(target, side, fill, stop) == want


# ---------------------------------------------------------------- 不变量
def buy(**kw):
    base = dict(order_id="A", setup_id="s", side="LONG", order_type="STOP_LIMIT_NEXT_BAR", stop_ticks=19984,
                target=TargetSpec("INITIAL_RISK_MULTIPLE", 2), armed_at_utc=5, expires_at_utc=300, flatten_at_utc=10_000,
                trigger_ticks=20000, limit_ticks=20004)
    base.update(kw)
    return F.WorkingOrder(**base)


def test_fill_never_beyond_limit():
    """不管入场滑点配多大，成交价都不越过限价。"""
    cfg = F.FillConfig(entry_slippage_ticks=50)
    for bar in (F.SubBar(60, 120, 19996, 20040, 19990, 20030), F.SubBar(60, 120, 20002, 20040, 19995, 20030),
                F.SubBar(60, 120, 20020, 20040, 20003, 20030)):
        book, ev = F.apply_bar(F.Book((buy(),)), bar, cfg)
        entry = next(e for e in ev if e.kind == "ENTRY")
        assert entry.price_ticks <= 20004
    book, ev = F.apply_bar(F.Book((buy(side="SHORT", trigger_ticks=20000, limit_ticks=19996, stop_ticks=20016),)),
                           F.SubBar(60, 120, 20004, 20010, 19960, 19970), cfg)
    assert next(e for e in ev if e.kind == "ENTRY").price_ticks >= 19996


def test_no_bars_no_events():
    book = F.Book((buy(),))
    assert F.expire_orders(book, 100) == (book, [])
    assert F.flatten_check(book, 100) == (book, [])


def test_bar_before_activation_is_invisible_to_order():
    book, ev = F.apply_bar(F.Book((buy(armed_at_utc=120),)), F.SubBar(60, 120, 19996, 20040, 19990, 20030))
    assert ev == [] and book.orders[0].status == "ARMED" and book.orders[0].bars_seen == 0


def test_same_side_order_does_not_add_to_position():
    pos = F.Position("pos_P", "P", "s", "LONG", 19990, 0, 19970, 19970, 20100, 10_000)
    book, ev = F.apply_bar(F.Book((buy(),), pos), F.SubBar(60, 120, 19996, 20010, 19995, 20008))
    assert [e.kind for e in ev] == ["TRIGGERED", "CANCELLED"] and ev[1].reason == "POSITION_OPEN_SAME_SIDE"
    assert book.position == pos


def test_invalid_after_fill_exits_immediately():
    """跳空成交价已越过止损：立即离场并标记，不留下方向错误的仓位。"""
    od = buy(order_type="MARKET_NEXT_OPEN", trigger_ticks=None, limit_ticks=None, stop_ticks=20010)
    book, ev = F.apply_bar(F.Book((od,)), F.SubBar(60, 120, 20000, 20004, 19996, 20002))
    assert [e.kind for e in ev] == ["ENTRY", "EXIT"] and ev[1].flags == ("INVALID_AFTER_FILL",)
    assert ev[1].price_ticks == 20000 and book.position is None


def test_reversal_when_old_stop_also_hit_is_stop_first():
    pos = F.Position("pos_P", "P", "s", "LONG", 20000, 0, 19984, 19984, 20032, 10_000)
    rev = buy(order_id="R", side="SHORT", trigger_ticks=19992, limit_ticks=19991, stop_ticks=20008)
    book, ev = F.apply_bar(F.Book((rev,), pos), F.SubBar(60, 120, 19996, 19998, 19980, 19990))
    kinds = [(e.kind, e.reason) for e in ev]
    assert ("EXIT", "STOP") in kinds and ("EXIT", "REVERSAL") not in kinds
    stop_exit = next(e for e in ev if e.kind == "EXIT" and e.order_id == "P")
    assert stop_exit.price_ticks == 19984 and "AMBIGUOUS_BAR" in stop_exit.flags


def test_cutoff_expiry_is_cancel():
    od = buy(expires_at_utc=300, flatten_at_utc=300)
    book, ev = F.expire_orders(F.Book((od,)), 300)
    assert (ev[0].kind, ev[0].reason, book.orders[0].status) == ("CANCELLED", "CUTOFF", "CANCELLED")
