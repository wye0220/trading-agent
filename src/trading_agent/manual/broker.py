"""券商只读适配器。接口只有读取方法；这里没有、也不允许有下单、改单、撤单的调用。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import contracts
from ..bars import decimal_to_ticks
from ..config import Settings
from ..sessions import parse_utc

STOP_KINDS = ("STOP", "STOP_LIMIT")


class BrokerReadError(Exception):
    """读取账户快照失败。"""


@dataclass(frozen=True)
class PositionView:
    contract_id: str
    quantity: int                    # 有符号：正为多，负为空
    avg_price_ticks: int | None = None
    unrealized_usd: Decimal | None = None

    @property
    def side(self) -> str:
        return "LONG" if self.quantity > 0 else "SHORT"


@dataclass(frozen=True)
class OrderView:
    order_id: str
    contract_id: str
    side: str                        # BUY | SELL
    kind: str                        # STOP | STOP_LIMIT | LIMIT | MARKET
    quantity: int
    price_ticks: int | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    fetched_at_utc: int
    source: str
    positions: tuple[PositionView, ...] = ()
    open_orders: tuple[OrderView, ...] = ()
    fills_today: tuple[dict, ...] = field(default_factory=tuple)

    def to_json(self) -> dict:
        return {"positions": [{"contract_id": p.contract_id, "quantity": p.quantity, "avg_price_ticks": p.avg_price_ticks,
                               "unrealized_usd": None if p.unrealized_usd is None else str(p.unrealized_usd)}
                              for p in self.positions],
                "open_orders": [{"order_id": o.order_id, "contract_id": o.contract_id, "side": o.side, "kind": o.kind,
                                 "quantity": o.quantity, "price_ticks": o.price_ticks} for o in self.open_orders],
                "fills_today": list(self.fills_today)}

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()


@runtime_checkable
class BrokerReadOnly(Protocol):
    """只读接口：只有 fetch。"""

    name: str

    def fetch(self, account_id: str) -> AccountSnapshot: ...


class FileSnapshotAdapter:
    """读 <data_root>/broker/<account_id>/snapshot.json。文件由你或别的工具写入，本适配器只读。

    格式：{"fetched_at_utc": ISO 时刻, "positions": [{"contract", "quantity", "avg_price", "unrealized_usd"}],
           "open_orders": [{"order_id", "contract", "side", "kind", "quantity", "price"}], "fills_today": [...]}
    价格是点数；quantity 在 positions 里有符号。
    """

    name = "file_snapshot"

    def __init__(self, settings: Settings):
        self._settings = settings

    def path(self, account_id: str) -> Path:
        return self._settings.data_root / "broker" / account_id / "snapshot.json"

    def fetch(self, account_id: str) -> AccountSnapshot:
        p = self.path(account_id)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            at = parse_utc(str(raw["fetched_at_utc"]))
            day = datetime.fromtimestamp(at, timezone.utc).date()

            def cid(text):
                from .common import resolve_contract
                return resolve_contract(self._settings, text, day)

            positions = []
            for x in raw.get("positions") or []:
                c, inst = cid(x["contract"])
                if int(x["quantity"]) == 0:
                    continue
                positions.append(PositionView(
                    c, int(x["quantity"]),
                    None if x.get("avg_price") is None else decimal_to_ticks(x["avg_price"], inst.tick_size),
                    None if x.get("unrealized_usd") is None else Decimal(str(x["unrealized_usd"]))))
            orders = []
            for x in raw.get("open_orders") or []:
                c, inst = cid(x["contract"])
                side, kind = str(x["side"]).upper(), str(x["kind"]).upper()
                if side not in ("BUY", "SELL") or kind not in ("STOP", "STOP_LIMIT", "LIMIT", "MARKET"):
                    raise ValueError(f"未知的方向或订单类型：{side} / {kind}")
                orders.append(OrderView(str(x["order_id"]), c, side, kind, int(x["quantity"]),
                                        None if x.get("price") is None else decimal_to_ticks(x["price"], inst.tick_size)))
            return AccountSnapshot(account_id, at, self.name, tuple(positions), tuple(orders),
                                   tuple(raw.get("fills_today") or ()))
        except FileNotFoundError:
            raise BrokerReadError(f"没有账户快照文件：{p}") from None
        except (ValueError, KeyError, TypeError) as e:
            raise BrokerReadError(f"账户快照文件无法解析：{e}") from None
