"""阶段 4 测试共用。"""
import json
from datetime import date

import helpers as H
import test_engine_runner as T
import test_risk as TR
from trading_agent import sessions
from trading_agent.engine import runner as RN
from trading_agent.manual import broker, config as MC, plans
from trading_agent.manual.common import paper_run_id

D1 = T.D1
ACCT = "acct1"
MAN = MC.load_manual(H.PROJECT)
FUNDED = TR.funded()                        # 费用 5，单笔预算 500，日损 1000，最多 5 手，已批准
UNFUNDED = T.RISK                           # 默认配置：限额未批准


def paper_state(env, upto: int, mirror: bool = False):
    """把脚本化的交易日喂给该 scope 的纸面运行，推进到第 upto 根。返回此刻的时间。"""
    rows = T.scripted_day()
    if mirror:
        ax = 5000 * 2
        for r in rows:
            o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            r.update(open=str(ax - o), high=str(ax - l), low=str(ax - h), close=str(ax - c))
    T.load(env, rows)
    run_id = paper_run_id(env.scope)
    RN.create_run(env.con, env.scope, T.PA, "paper", run_id)
    for i in range(-6, upto + 1):
        RN.step(env.settings, env.con, run_id, T.PA, T.bar_end(i) + 5, risk_cfg=T.RISK, engine_cfg=T.ENG)
    return T.bar_end(upto) + 10


def quick(env, text: str):
    return plans.parse_quick(text.split(), env.settings, D1)


def mkplan(env, text="ES L 5000 4996 5008 1 test", *, now, risk=FUNDED, account=ACCT, fetched=True, **kw):
    return plans.create(env.settings, env.con, account_id=account, scope=env.scope, strategy=T.PA, inp=quick(env, text),
                        risk_cfg=risk, manual_cfg=MAN, now_utc=now, snapshot_fetched_at=now - 5 if fetched else None, **kw)


def activate(env, plan_id, *, now, risk=FUNDED):
    return plans.activate(env.settings, env.con, plan_id, scope=env.scope, strategy=T.PA, risk_cfg=risk,
                          manual_cfg=MAN, now_utc=now, snapshot_fetched_at=now - 5)


TRADE_COLS = ["account_id", "contract", "side", "quantity", "executed_at_utc", "price", "source", "broker_order_id",
              "broker_fill_id", "plan_id"]
EVENT_COLS = ["account_id", "broker_order_id", "event_type", "event_time_utc", "source", "contract", "side",
              "order_kind", "price", "quantity"]


def trade(at, side, price, qty=1, *, fill=None, order=None, plan=None, account=ACCT, contract="ESZ5"):
    return {"account_id": account, "contract": contract, "side": side, "quantity": qty,
            "executed_at_utc": sessions.iso(at), "price": price, "source": "broker_csv", "broker_order_id": order or "",
            "broker_fill_id": fill or "", "plan_id": plan or ""}


def oevent(at, order, etype, kind, price=None, *, side="SELL", account=ACCT, contract="ESZ5", qty=1):
    return {"account_id": account, "broker_order_id": order, "event_type": etype, "event_time_utc": sessions.iso(at),
            "source": "broker_csv", "contract": contract, "side": side, "order_kind": kind,
            "price": "" if price is None else price, "quantity": qty}


def write(env, name, rows, cols):
    return H.write_csv(env.tmp / name, rows, columns=cols)


def do_import(env, trades=None, events=None, *, risk=FUNDED, name="t"):
    from trading_agent.manual import trades as TM
    out = {}
    if events is not None:
        out["events"] = TM.import_order_events(env.settings, env.con, write(env, f"{name}_events.csv", events, EVENT_COLS))
    if trades is not None:
        out["trades"] = TM.import_trades(env.settings, env.con, write(env, f"{name}_trades.csv", trades, TRADE_COLS),
                                         risk_cfg=risk, manual_cfg=MAN)
    return out


def snapshot(env, at, positions=(), orders=(), account=ACCT):
    p = env.root / "broker" / account / "snapshot.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fetched_at_utc": sessions.iso(at), "positions": list(positions),
                             "open_orders": list(orders)}), encoding="utf-8")
    return p


def pos(qty, avg=5000, contract="ESZ5", unrealized=None):
    d = {"contract": contract, "quantity": qty, "avg_price": avg}
    if unrealized is not None:
        d["unrealized_usd"] = unrealized
    return d


def stop_order(price, side="SELL", oid="S1", qty=1, kind="STOP", contract="ESZ5"):
    return {"order_id": oid, "contract": contract, "side": side, "kind": kind, "quantity": qty, "price": price}


def poll(env, now, interval=60, risk=FUNDED, man=MAN, account=ACCT, adapter=None):
    from trading_agent.manual import watch as W
    return W.poll_once(env.settings, env.con, adapter or broker.FileSnapshotAdapter(env.settings), account,
                       scope=env.scope, strategy=T.PA, risk_cfg=risk, manual_cfg=man, interval_seconds=interval,
                       now_utc=now)
