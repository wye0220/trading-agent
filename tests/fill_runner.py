"""成交向量的运行器。

向量是 YAML：
    name, description, date, slippage: {entry, stop}, market_fill_on_straddle, flatten_at ("HH:MM" ET，缺省 15:55),
    position: {side, entry, stop, target, entry_time}            # 可选：开始时已有的仓位
    orders: [{id, side, type, trigger, limit, stop, target: [kind, value], armed, expires}]
    steps: [{bar: {t, len, b: [o,h,l,c]}} | {expire: "HH:MM:SS"} | {flatten_check: "HH:MM:SS"}]
    expect: {events: [{kind, order, price, reason, flags}], orders: {id: 状态}, position: null | {...}}

价格都是点数（tick 0.25）。时间是纽约时间；bar 的 t 可带日期前缀 "YYYY-MM-DD HH:MM"。
镜像：价格关于 mirror_axis（缺省 5000）翻转，方向互换，再跑一遍。
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import yaml

from trading_agent import sessions
from trading_agent.engine import fills as F
from trading_agent.strategies.base import TargetSpec

VECTOR_DIR = Path(__file__).parent / "vectors" / "fills"
TICK = Decimal("0.25")
FLIP = {"LONG": "SHORT", "SHORT": "LONG"}


def ticks(points) -> int:
    q = Decimal(str(points)) / TICK
    assert q == q.to_integral_value(), f"{points} 不在 tick 上"
    return int(q)


def all_vectors() -> list[Path]:
    return sorted(VECTOR_DIR.glob("*.yaml"))


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        v = yaml.safe_load(f)
    v["_path"] = path.name
    return v


def _t(day: date, text: str) -> int:
    text = str(text)
    if " " in text:
        d, text = text.split(" ")
        day = date.fromisoformat(d)
    parts = [int(x) for x in text.split(":")] + [0]
    return sessions.ts(datetime.combine(day, time(parts[0], parts[1], parts[2]), sessions.NY))


class Ctx:
    def __init__(self, vec: dict, mirror: bool):
        self.day = date.fromisoformat(str(vec["date"]))
        self.mirror = mirror
        self.axis = ticks(vec.get("mirror_axis", 5000))
        self.flatten = _t(self.day, vec.get("flatten_at", "15:55"))

    def px(self, points):
        if points is None:
            return None
        t = ticks(points)
        return 2 * self.axis - t if self.mirror else t

    def side(self, s):
        return FLIP[s] if self.mirror else s

    def target(self, spec) -> TargetSpec:
        kind, value = spec
        if kind == "ABSOLUTE_PRICE_TICKS":
            return TargetSpec(kind, self.px(value))
        if kind == "OFFSET_FROM_FILL_TICKS":
            return TargetSpec(kind, int(value))
        return TargetSpec(kind, Fraction(str(value)) if not isinstance(value, int) else value)

    def bar(self, b) -> F.SubBar:
        start = _t(self.day, b["t"])
        o, h, l, c = (ticks(x) for x in b["b"])
        if self.mirror:
            a = self.axis
            o, h, l, c = 2 * a - o, 2 * a - l, 2 * a - h, 2 * a - c
        return F.SubBar(start, start + int(b.get("len", 60)), o, h, l, c)


def run(vec: dict, mirror: bool = False):
    """跑一个向量并核对期望。返回 (最终订单簿, 事件列表)。"""
    c = Ctx(vec, mirror)
    slip = vec.get("slippage") or {}
    cfg = F.FillConfig(int(slip.get("entry", 0)), int(slip.get("stop", 0)), bool(vec.get("market_fill_on_straddle", False)))
    orders = []
    for o in vec.get("orders") or []:
        orders.append(F.WorkingOrder(
            order_id=str(o["id"]), setup_id="s-" + str(o["id"]), side=c.side(o["side"]), order_type=o["type"],
            stop_ticks=c.px(o["stop"]), target=c.target(o["target"]), armed_at_utc=_t(c.day, o["armed"]),
            expires_at_utc=min(_t(c.day, o["expires"]), c.flatten), flatten_at_utc=c.flatten,
            trigger_ticks=c.px(o.get("trigger")), limit_ticks=c.px(o.get("limit"))))
    pos = None
    if vec.get("position"):
        p = vec["position"]
        pos = F.Position("pos_P", "P", "s-P", c.side(p["side"]), c.px(p["entry"]), _t(c.day, p.get("entry_time", "09:35")),
                         c.px(p["stop"]), c.px(p["stop"]), c.px(p["target"]), c.flatten)
    book, events = F.Book(tuple(orders), pos), []
    for st in vec["steps"]:
        if "bar" in st:
            book, ev = F.apply_bar(book, c.bar(st["bar"]), cfg)
        elif "expire" in st:
            book, ev = F.expire_orders(book, _t(c.day, st["expire"]))
        else:
            book, ev = F.flatten_check(book, _t(c.day, st["flatten_check"]))
        events += ev

    exp = vec["expect"]
    where = f"{vec['_path']}{'（镜像）' if mirror else ''}"
    got = [{"kind": e.kind, "order": e.order_id, "price": e.price_ticks, "reason": e.reason, "flags": sorted(e.flags)}
           for e in events]
    want = exp.get("events")
    if want is not None:
        assert len(got) == len(want), f"{where}：事件数 期望 {len(want)}，得到 {len(got)}：{got}"
        for i, (g, w) in enumerate(zip(got, want)):
            for k, v in w.items():
                if k == "price":
                    v = c.px(v)
                elif k == "flags":
                    v = sorted(v)
                elif k == "order":
                    v = str(v)
                assert g[k] == v, f"{where} 第 {i} 个事件 [{k}]：期望 {v!r}，得到 {g[k]!r}（{g}）"
    for oid, status in (exp.get("orders") or {}).items():
        actual = next(o.status for o in book.orders if o.order_id == str(oid))
        assert actual == status, f"{where} 订单 {oid}：期望 {status}，得到 {actual}"
    if "position" in exp:
        p = book.position
        if exp["position"] is None:
            assert p is None, f"{where}：期望没有仓位，得到 {p}"
        else:
            assert p is not None, f"{where}：期望有仓位"
            w = exp["position"]
            for k, attr in (("entry", "entry_ticks"), ("stop", "stop_ticks"), ("target", "target_ticks")):
                if k in w:
                    assert getattr(p, attr) == c.px(w[k]), f"{where} 仓位 [{k}]：期望 {c.px(w[k])}，得到 {getattr(p, attr)}"
            if "side" in w:
                assert p.side == c.side(w["side"]), f"{where} 仓位方向"
            if "flags" in w:
                assert sorted(p.flags) == sorted(w["flags"]), f"{where} 仓位标记：{p.flags}"
    return book, events
