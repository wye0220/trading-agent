"""Alpaca 模拟盘的只读适配器：持仓、挂单、成交和订单记录。

只读。这里只做解析，没有网络代码，没有、也不允许有下单、改单、撤单的调用。
两种取数方式，解析完全相同：
- sources/alpaca_rest.py 的 RestTransport：用 API 密钥直接读（本模块不含任何网络代码）。
- DumpTransport：读事先保存的 JSON 文件（例如 Claude 会话里用 Alpaca 连接器取回后存下的），
  放在 <data_root>/broker/<账户>/alpaca/ 下：positions.json、orders_open.json、orders_all.json、fills.json。

只处理已登记的品种（如 SPY、QQQ）；账户里的其他代码忽略并计数。
"""
from __future__ import annotations

import csv
import json
import re
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from .. import sessions
from ..config import Settings
from .broker import AccountSnapshot, BrokerReadError, OrderView, PositionView
from .common import is_equity

SOURCE = "alpaca"
_PLAN_ID = re.compile(r"plan_[0-9a-f]{12}")
KINDS = {"market": "MARKET", "limit": "LIMIT", "stop": "STOP", "stop_limit": "STOP_LIMIT", "trailing_stop": "STOP"}
TRADE_COLUMNS = ["account_id", "contract", "side", "quantity", "executed_at_utc", "price", "source",
                 "broker_order_id", "broker_fill_id", "plan_id"]
EVENT_COLUMNS = ["account_id", "broker_order_id", "event_type", "event_time_utc", "source", "contract", "side",
                 "order_kind", "price", "quantity"]


class DumpTransport:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def get(self, name: str):
        p = self.directory / f"{name}.json"
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise BrokerReadError(f"没有文件：{p}") from None
        except ValueError as e:
            raise BrokerReadError(f"{p} 不是合法的 JSON：{e}") from None
        return _unwrap(raw), int(p.stat().st_mtime)


def _unwrap(raw):
    """连接器的返回外面包了一两层（data、result）；REST 的返回直接是列表。"""
    for _ in range(3):
        if isinstance(raw, dict):
            for k in ("data", "result"):
                if k in raw:
                    raw = raw[k]
                    break
            else:
                break
    if not isinstance(raw, list):
        raise BrokerReadError("返回内容不是列表")
    return raw


def _ticks(price, inst) -> int:
    """价格换成整数 tick。Alpaca 的均价和成交价可能带到小数点后四位，按最近的 tick 取整。"""
    return int((Decimal(str(price)) / inst.tick_size).to_integral_value(ROUND_HALF_EVEN))


def _qty(text) -> int | None:
    q = Decimal(str(text))
    return int(q) if q == q.to_integral_value() else None        # 碎股不处理


class AlpacaReadOnly:
    name = SOURCE

    def __init__(self, settings: Settings, transport):
        self._settings, self._t = settings, transport
        self.ignored: dict[str, int] = {}

    def _inst(self, symbol: str):
        inst = self._settings.instruments.get(str(symbol).upper())
        if inst is None or not is_equity(self._settings, inst):
            self.ignored[str(symbol)] = self.ignored.get(str(symbol), 0) + 1
            return None
        return inst

    def _get(self, name: str):
        try:
            return self._t.get(name)
        except BrokerReadError:
            raise
        except Exception as e:  # noqa: BLE001 — 网络层的错误（消息不含凭证）统一成读取失败
            raise BrokerReadError(str(e)) from None

    def fetch(self, account_id: str) -> AccountSnapshot:
        self.ignored = {}
        try:
            pos_raw, at1 = self._get("positions")
            ord_raw, at2 = self._get("orders_open")
            positions, orders = [], []
            for p in pos_raw:
                inst = self._inst(p["symbol"])
                q = _qty(p["qty"])
                if inst is None or not q:
                    continue
                q = -abs(q) if str(p.get("side", "long")).lower() == "short" else abs(q)
                positions.append(PositionView(
                    inst.root, q, None if p.get("avg_entry_price") is None else _ticks(p["avg_entry_price"], inst),
                    None if p.get("unrealized_pl") is None else Decimal(str(p["unrealized_pl"]))))
            for o in ord_raw:
                inst = self._inst(o["symbol"])
                q = _qty(o.get("qty") or 0)
                kind = KINDS.get(str(o.get("type") or o.get("order_type")).lower())
                if inst is None or not q or kind is None:
                    continue
                price = o.get("stop_price") if kind in ("STOP", "STOP_LIMIT") else o.get("limit_price")
                orders.append(OrderView(str(o["id"]), inst.root, "BUY" if str(o["side"]).lower() == "buy" else "SELL",
                                        kind, q, None if price is None else _ticks(price, inst)))
            return AccountSnapshot(account_id, min(at1, at2), self.name, tuple(positions), tuple(orders), ())
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            raise BrokerReadError(f"Alpaca 返回的内容无法解析：{e}") from None


    # ------------------------------------------------------------ 成交与订单记录（转成导入用的行）
    def _orders(self) -> list[dict]:
        return self._get("orders_all")[0]

    @staticmethod
    def _final_id(orders: dict[str, dict], oid: str) -> str:
        """改单在 Alpaca 里是"替换成一张新订单"。顺着 replaced_by 找到这条链最后的那张，作为整条链的订单号。"""
        seen = set()
        while oid in orders and orders[oid].get("replaced_by") and oid not in seen:
            seen.add(oid)
            oid = str(orders[oid]["replaced_by"])
        return oid

    def trade_rows(self, account_id: str) -> list[dict]:
        """成交行。有逐笔成交记录（fills）就用它；没有就用订单记录里已成交的订单，一张订单算一笔。"""
        orders = self._orders()
        # 计划关联的凭据：下单时把计划编号写进订单的 client_order_id（任意位置含 plan_xxxxxxxxxxxx 即可）
        plan_of = {}
        for o in orders:
            m = _PLAN_ID.search(str(o.get("client_order_id") or ""))
            if m:
                plan_of[str(o["id"])] = m.group(0)
        try:
            fills = [(f["symbol"], f["qty"], f["side"], f["price"], f["transaction_time"], str(f.get("order_id") or ""),
                      str(f["id"])) for f in self._get("fills")[0]]
        except BrokerReadError:
            fills = [(o["symbol"], o["filled_qty"], o["side"], o["filled_avg_price"], o["filled_at"], str(o["id"]),
                      "order:" + str(o["id"])) for o in orders
                     if o.get("filled_at") and o.get("filled_avg_price") and Decimal(str(o.get("filled_qty") or 0)) > 0]
        rows = []
        for symbol, qty, side, price, at, order_id, fill_id in fills:
            inst = self._inst(symbol)
            q = _qty(qty)
            if inst is None or not q:
                continue
            rows.append({"account_id": account_id, "contract": inst.root,
                         "side": "BUY" if str(side).lower() == "buy" else "SELL", "quantity": str(q),
                         "executed_at_utc": sessions.iso(sessions.parse_utc(at)),
                         "price": str(Decimal(_ticks(price, inst)) * inst.tick_size), "source": SOURCE,
                         "broker_order_id": order_id, "broker_fill_id": fill_id,
                         "plan_id": plan_of.get(order_id, "")})
        return sorted(rows, key=lambda r: (r["executed_at_utc"], r["broker_fill_id"]))

    def order_event_rows(self, account_id: str) -> list[dict]:
        """订单事件行。一条改单链记在同一个订单号下：最初那张是 SUBMITTED，之后每次替换是 MODIFIED（带新价格）。"""
        raw = self._orders()
        by_id = {str(o["id"]): o for o in raw}
        rows = []
        for o in raw:
            inst = self._inst(o["symbol"])
            kind = KINDS.get(str(o.get("type") or o.get("order_type")).lower())
            q = _qty(o.get("qty") or 0)
            if inst is None or kind is None or not q:
                continue
            price = o.get("stop_price") if kind in ("STOP", "STOP_LIMIT") else o.get("limit_price")
            base = {"account_id": account_id, "broker_order_id": self._final_id(by_id, str(o["id"])), "source": SOURCE,
                    "contract": inst.root, "side": "BUY" if str(o["side"]).lower() == "buy" else "SELL",
                    "order_kind": kind,
                    "price": "" if price is None else str(Decimal(_ticks(price, inst)) * inst.tick_size),
                    "quantity": str(q)}
            first = "MODIFIED" if o.get("replaces") else "SUBMITTED"
            for field, event in (("submitted_at", first), ("canceled_at", "CANCELLED"), ("filled_at", "FILLED"),
                                 ("expired_at", "EXPIRED")):
                if o.get(field):
                    rows.append({**base, "event_type": event,
                                 "event_time_utc": sessions.iso(sessions.parse_utc(o[field]))})
        return sorted(rows, key=lambda r: (r["event_time_utc"], r["broker_order_id"], r["event_type"]))


def dump_dir(settings: Settings, account_id: str) -> Path:
    return settings.data_root / "broker" / account_id / "alpaca"


def adapter_for(settings: Settings, account_id: str, env: dict | None = None) -> AlpacaReadOnly:
    """有 API 密钥就直接读；否则读保存下来的 JSON 文件。"""
    from ..sources import alpaca_rest
    cred = alpaca_rest.credentials(settings.project_root, env, settings.data_root / "secrets")
    transport = alpaca_rest.RestTransport(*cred) if cred else DumpTransport(dump_dir(settings, account_id))
    return AlpacaReadOnly(settings, transport)


def _write(path: Path, columns: list[str], rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return path


def sync(settings: Settings, con, adapter: AlpacaReadOnly, account_id: str, *, risk_cfg, manual_cfg) -> dict:
    """把 Alpaca 的订单记录和成交导入人工账本。走与 CSV 导入相同的函数，所以去重、开平判定、计划关联都一样。"""
    from . import trades as trades_mod
    events, fills = adapter.order_event_rows(account_id), adapter.trade_rows(account_id)
    base = settings.data_root / "broker" / account_id / "import"
    out = {"account_id": account_id, "source": SOURCE, "results": [], "ignored_symbols": dict(adapter.ignored)}
    out["results"].append(trades_mod.import_order_events(settings, con, _write(base / "alpaca_order_events.csv",
                                                                             EVENT_COLUMNS, events)))
    out["results"].append(trades_mod.import_trades(settings, con, _write(base / "alpaca_trades.csv", TRADE_COLUMNS, fills),
                                                   risk_cfg=risk_cfg, manual_cfg=manual_cfg))
    return out
