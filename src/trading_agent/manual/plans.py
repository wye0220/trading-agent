"""人工交易计划：登记、预检、激活、取消、过期。只给结论，不下单。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import date

from .. import contracts, db, sessions
from ..bars import OffTickPrice, decimal_to_ticks
from ..config import ConfigError, Scope, Settings
from ..engine import llm, risk as risk_mod, versions
from ..engine.config import RiskConfig
from ..strategies.base import TraderPlan
from . import common
from .common import last_closed, strategy_view
from .config import ManualConfig

ACTIVE_STATES = ("ACTIVE", "PARTIALLY_FILLED")
OPEN_STATES = ("DRAFT", "ACTIVE", "PARTIALLY_FILLED")
SIDES = {"L": "LONG", "LONG": "LONG", "B": "LONG", "BUY": "LONG", "S": "SHORT", "SHORT": "SHORT", "SELL": "SHORT"}


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class PlanInput:
    instrument_root: str
    contract_id: str
    side: str
    entry_ticks: int
    stop_ticks: int | None
    target_ticks: int | None
    quantity: int
    reason: str


def parse_quick(tokens: list[str], settings: Settings, today: date) -> PlanInput:
    """一行快捷语法：根代码 方向 入场 止损 目标 手数 理由…。止损、目标可写 - 表示没有。"""
    names = ["根代码", "方向", "入场", "止损", "目标", "手数"]
    if len(tokens) < 6:
        raise PlanError("计划缺少字段：" + "、".join(names[len(tokens):])
                        + "。写法：plan ES L 5000 4995 5010 1 理由")
    root, side_t, entry_t, stop_t, target_t, qty_t = tokens[:6]
    root = root.upper()
    try:
        inst = settings.instrument(root)
    except ConfigError as e:
        raise PlanError(str(e)) from None
    side = SIDES.get(side_t.upper())
    if side is None:
        raise PlanError(f"方向只能是 L 或 S：{side_t!r}")

    def px(text, name, optional=False):
        if optional and text in ("-", "", "none", "None"):
            return None
        try:
            return decimal_to_ticks(text, inst.tick_size)
        except OffTickPrice as e:
            raise PlanError(f"{name}价不合法：{e}") from None

    entry, stop, target = px(entry_t, "入场"), px(stop_t, "止损", True), px(target_t, "目标", True)
    try:
        qty = int(qty_t)
        if qty <= 0:
            raise ValueError
    except ValueError:
        raise PlanError(f"手数应为正整数：{qty_t!r}") from None
    d = 1 if side == "LONG" else -1
    if stop is not None and d * (entry - stop) <= 0:
        raise PlanError("止损在入场的错误一侧")
    if target is not None and d * (target - entry) <= 0:
        raise PlanError("目标在入场的错误一侧")
    cid = common.active_contract(settings, inst, today)
    return PlanInput(root, cid, side, entry, stop, target, qty, " ".join(tokens[6:]).strip())


# ---------------------------------------------------------------- 状态变化
def _event(con, plan_id, frm, to, actor, **detail):
    con.execute("INSERT INTO plan_events (plan_id, from_status, to_status, at_utc, actor, detail_json)"
                " VALUES (?,?,?,?,?,?)", (plan_id, frm, to, detail.pop("at_utc", int(time.time())), actor,
                                          json.dumps(detail, ensure_ascii=False)))


def get(con: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
    r = con.execute("SELECT * FROM trader_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if r is None:
        raise PlanError(f"没有这份计划：{plan_id}")
    return r


def _transition(con, plan: sqlite3.Row, to: str, actor: str, now: int, **fields) -> None:
    db.versioned_update(con, "trader_plans", "plan_id", plan["plan_id"], plan["version"], status=to,
                        updated_at_utc=now, **fields)
    _event(con, plan["plan_id"], plan["status"], to, actor, at_utc=now)


def expire_due(con: sqlite3.Connection, now_utc: int, account_id: str | None = None) -> list[str]:
    """把过了失效时间仍未成交的计划转为 EXPIRED，并释放预留。"""
    q = "SELECT * FROM trader_plans WHERE status IN ('DRAFT','ACTIVE') AND expires_at_utc IS NOT NULL AND expires_at_utc<=?"
    args: tuple = (now_utc,)
    if account_id:
        q, args = q + " AND account_id=?", (now_utc, account_id)
    out = []
    for p in con.execute(q, args).fetchall():
        risk_mod.release(con, p["plan_id"])
        with con:
            _transition(con, get(con, p["plan_id"]), "EXPIRED", "system", now_utc)
        out.append(p["plan_id"])
    return out


def was_active_at(con: sqlite3.Connection, plan_id: str, at_utc: int) -> bool:
    """该计划在 at_utc 时是否处于已激活（且未结束）状态。依据审计记录，不看现在的状态。"""
    p = con.execute("SELECT expires_at_utc FROM trader_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if p is None or (p["expires_at_utc"] is not None and p["expires_at_utc"] <= at_utc):
        return False
    state = None
    for e in con.execute("SELECT to_status FROM plan_events WHERE plan_id=? AND at_utc<=? ORDER BY at_utc, event_id",
                         (plan_id, at_utc)):
        state = e["to_status"]
    return state in ACTIVE_STATES


# ---------------------------------------------------------------- 预检
def precheck(settings: Settings, con: sqlite3.Connection, *, plan_id: str, account_id: str, scope: Scope, strategy,
             inp: PlanInput, risk_cfg: RiskConfig, manual_cfg: ManualConfig, now_utc: int,
             snapshot_fetched_at: int | None = None, snapshot_complete: bool = True) -> dict:
    """三部分各自给出，再合成结论。返回符合 PretradeCheck schema 的对象，外加 detail。"""
    inst = settings.instrument(inp.instrument_root)
    day = sessions.trading_day_at(now_utc).isoformat()

    # 1) 策略一致性
    view = strategy_view(settings, con, scope, strategy, now_utc, manual_cfg.strategy_state_max_age_bars)
    flags: list[str] = []
    setup_id = None
    conditions: list[str] = []
    no_strategy = common.is_equity(settings, inst)
    if no_strategy:
        # 股票类品种没有正式策略和基线：不拿别的品种的信号去套，只评价计划纪律和账户风险
        view = None
        alignment, align_ev = "UNKNOWN", {"why": "该品种没有正式策略 scope，不评价策略一致性"}
        conditions.append("该品种没有正式策略，只评价计划纪律和账户风险")
    elif view is None:
        alignment, align_ev = "UNKNOWN", {"why": "该 scope 没有可用的策略状态（纸面运行不存在或已过期）"}
        conditions.append("策略状态不可用，无法判断与策略是否一致")
    else:
        state, ctx = view
        res = strategy.evaluate_pretrade_alignment(
            ctx, state, TraderPlan(inp.side, inp.entry_ticks, inp.stop_ticks, inp.target_ticks, inp.quantity,
                                   inp.contract_id, inp.reason))
        alignment, align_ev, setup_id = res.alignment, dict(res.evidence), res.setup_id
        if "COUNTERTREND_BEFORE_CONFIRMATION" in res.flags:
            flags.append("COUNTERTREND_BEFORE_CONFIRMATION")
        align_ev["flags"] = list(res.flags)
        conditions += [f"{k}={v}" for k, v in align_ev.items() if k != "flags" and v is not None]
    if inp.stop_ticks is None:
        flags.append("NO_STOP")

    # 2) 无新形态的重试：当日最近一笔平仓是止损，同合约同方向，而这份计划没有对应新的 setup
    prev = last_closed(con, account_id, day, now_utc)
    if (prev is not None and prev["exit_kind"] == "STOP" and prev["contract_id"] == inp.contract_id
            and prev["side"] == inp.side and setup_id is None):     # side 记的是仓位方向
        flags.append("RETRY_WITHOUT_NEW_SETUP")

    # 3) 账户风险
    st = risk_mod.account_state(con, account_id, day, fetched_at_utc=snapshot_fetched_at, complete=snapshot_complete)
    dist = None if inp.stop_ticks is None else abs(inp.entry_ticks - inp.stop_ticks)
    dec = risk_mod.check_manual_plan(inst, dist, risk_cfg, st, now_utc, inp.quantity)
    if dec.reason_code in ("RISK_OVER_BUDGET", "MIN_SIZE_OVER_BUDGET"):
        flags.append("RISK_OVER_BUDGET")
    if dec.reason_code == "DAILY_LIMIT_HIT":
        flags.append("DAILY_LIMIT_HIT")

    # 控制通道的当日约束（只能收紧）：禁止新入场、手数上限（阶段 10）
    from ..ops import context as ctxmod
    risk_status, risk_reason = dec.status, dec.reason_code
    blocked = not dec.passed
    if ctxmod.no_entry(con, str(scope), now_utc):
        blocked, risk_status, risk_reason = True, "BLOCKED", "CONTEXT_NO_ENTRY"
    else:
        cap = ctxmod.max_qty(con, str(scope), now_utc)
        if cap is not None and inp.quantity > cap:
            blocked, risk_status, risk_reason = True, "BLOCKED", f"CONTEXT_MAX_QTY:{cap}"
    conclusion = "BLOCKED_BY_RISK" if blocked else ("ALIGNED" if alignment == "ALIGNED" else "DEVIATES")
    tick = inst.tick_size
    px = lambda t: "无" if t is None else str(t * tick)       # noqa: E731
    summary = (f"{inp.contract_id} {'做多' if inp.side == 'LONG' else '做空'} {inp.quantity} {common.unit(settings, inst)}，入场 {px(inp.entry_ticks)}，"
               f"止损 {px(inp.stop_ticks)}，目标 {px(inp.target_ticks)}。结论 {conclusion}：策略一致性 {alignment}"
               + ("（该品种没有正式策略，不评价）" if no_strategy else "") + "，"
               f"账户风险 {risk_status}" + (f"（{risk_reason}）" if risk_reason else "")
               + ("；纪律标记 " + "、".join(flags) if flags else "") + "。预检只给结论，不下单。")
    check = {
        "plan_id": plan_id, "account_id": account_id, "scope": str(scope), "setup_id": setup_id,
        "conclusion": conclusion, "strategy_alignment": alignment, "risk_status": risk_status,
        "risk_reason": risk_reason, "discipline_flags": flags, "observable_conditions": conditions[:10],
        "data_as_of_utc": sessions.iso(now_utc), "summary": summary[:800],
    }
    errs = llm.schema_errors(settings.project_root, "PretradeCheck", check)
    if errs:                                             # 确定性代码生成的对象不该不合格；不合格就是缺陷
        raise PlanError("预检结果不符合 PretradeCheck schema：" + "；".join(errs[:3]))
    return {"check": check, "risk": {"quantity_allowed": dec.quantity, "risk_per_contract_usd": str(dec.risk_per_contract_usd),
                                     "detail": dec.detail, "account_version": st.version,
                                     "snapshot_fetched_at_utc": snapshot_fetched_at},
            "alignment_evidence": align_ev}


def create(settings: Settings, con: sqlite3.Connection, *, account_id: str, scope: Scope, strategy, inp: PlanInput,
           risk_cfg: RiskConfig, manual_cfg: ManualConfig, now_utc: int | None = None,
           snapshot_fetched_at: int | None = None, plan_id: str | None = None) -> dict:
    """登记一份计划（DRAFT）并预检。不预留预算。"""
    now = int(time.time()) if now_utc is None else now_utc
    expire_due(con, now, account_id)
    plan_id = plan_id or "plan_" + uuid.uuid4().hex[:12]
    pre = precheck(settings, con, plan_id=plan_id, account_id=account_id, scope=scope, strategy=strategy, inp=inp,
                   risk_cfg=risk_cfg, manual_cfg=manual_cfg, now_utc=now, snapshot_fetched_at=snapshot_fetched_at)
    from ..memory import store as memory_store
    mem_head = memory_store.head(con)
    vt = versions.current(scope, strategy, risk_cfg, memory_version=mem_head["memory_version"] if mem_head else None)
    expires = now + manual_cfg.plan_ttl_minutes * 60
    with con:
        con.execute(
            "INSERT INTO trader_plans (plan_id, account_id, strategy_family, contract_id, side, entry_ticks, stop_ticks,"
            " target_ticks, quantity, reason, status, expires_at_utc, pretrade_json, submitted_at_utc, updated_at_utc,"
            " scope, setup_id, version_tuple_json) VALUES (?,?,?,?,?,?,?,?,?,?,'DRAFT',?,?,?,?,?,?,?)",
            (plan_id, account_id, strategy.strategy_family, inp.contract_id, inp.side, inp.entry_ticks, inp.stop_ticks,
             inp.target_ticks, inp.quantity, inp.reason, expires, json.dumps(pre, ensure_ascii=False), now, now,
             str(scope), pre["check"]["setup_id"], json.dumps(vt)))
        _event(con, plan_id, None, "DRAFT", "trader", at_utc=now, conclusion=pre["check"]["conclusion"])
    return {"plan_id": plan_id, "status": "DRAFT", "account_id": account_id, "contract_id": inp.contract_id,
            "instrument_root": inp.instrument_root, "side": inp.side, "quantity": inp.quantity,
            "expires_at_utc": sessions.iso(expires), "reserved": False, **pre}


def _input_of(settings: Settings, plan: sqlite3.Row) -> PlanInput:
    return PlanInput(common.instrument_of(settings, plan["contract_id"]).root, plan["contract_id"], plan["side"], plan["entry_ticks"],
                     plan["stop_ticks"], plan["target_ticks"], plan["quantity"], plan["reason"] or "")


def activate(settings: Settings, con: sqlite3.Connection, plan_id: str, *, scope: Scope, strategy,
             risk_cfg: RiskConfig, manual_cfg: ManualConfig, now_utc: int | None = None,
             snapshot_fetched_at: int | None = None) -> dict:
    """重新预检；风险通过则预留并转为 ACTIVE。风险不通过时计划保持 DRAFT。"""
    now = int(time.time()) if now_utc is None else now_utc
    expire_due(con, now)
    plan = get(con, plan_id)
    if plan["status"] != "DRAFT":
        raise PlanError(f"只有 DRAFT 的计划能激活：{plan_id} 现在是 {plan['status']}")
    inp = _input_of(settings, plan)
    pre = precheck(settings, con, plan_id=plan_id, account_id=plan["account_id"], scope=scope, strategy=strategy,
                   inp=inp, risk_cfg=risk_cfg, manual_cfg=manual_cfg, now_utc=now, snapshot_fetched_at=snapshot_fetched_at)
    out = {"plan_id": plan_id, "account_id": plan["account_id"], "contract_id": plan["contract_id"], **pre}
    if pre["check"]["risk_status"] != "PASS":
        return {**out, "status": "DRAFT", "reserved": False}
    day = sessions.trading_day_at(now).isoformat()
    dec = risk_mod.reserve(con, settings.instrument(inp.instrument_root), risk_cfg, account_id=plan["account_id"],
                           trading_day=day, plan_id=plan_id, stop_distance_ticks=abs(inp.entry_ticks - inp.stop_ticks),
                           quantity=inp.quantity, expected_version=pre["risk"]["account_version"], now_utc=now,
                           fetched_at_utc=snapshot_fetched_at, strategy_family=plan["strategy_family"])
    if not dec.passed:
        return {**out, "status": "DRAFT", "reserved": False}
    with con:
        _transition(con, get(con, plan_id), "ACTIVE", "trader", now, activated_at_utc=now,
                    pretrade_json=json.dumps(pre, ensure_ascii=False), setup_id=pre["check"]["setup_id"])
    return {**out, "status": "ACTIVE", "reserved": True}


def cancel(con: sqlite3.Connection, plan_id: str, now_utc: int | None = None) -> dict:
    now = int(time.time()) if now_utc is None else now_utc
    plan = get(con, plan_id)
    if plan["status"] not in OPEN_STATES:
        raise PlanError(f"{plan_id} 已是 {plan['status']}，不能取消")
    risk_mod.release(con, plan_id)
    with con:
        _transition(con, get(con, plan_id), "CANCELLED", "trader", now)
    return {"plan_id": plan_id, "status": "CANCELLED"}


def record_fill(con: sqlite3.Connection, plan_id: str, quantity: int, at_utc: int) -> str:
    """把一笔已关联的开仓成交计入计划。满额时释放预留。调用方负责事务。"""
    plan = get(con, plan_id)
    filled = plan["filled_quantity"] + quantity
    to = "FILLED" if filled >= plan["quantity"] else "PARTIALLY_FILLED"
    db.versioned_update(con, "trader_plans", "plan_id", plan_id, plan["version"], status=to, filled_quantity=filled,
                        updated_at_utc=at_utc)
    _event(con, plan_id, plan["status"], to, "import", at_utc=at_utc, filled_quantity=filled)
    return to


def list_plans(con: sqlite3.Connection, account_id: str | None = None, only_open: bool = False) -> list[dict]:
    q, args = "SELECT * FROM trader_plans WHERE 1=1", []
    if account_id:
        q, args = q + " AND account_id=?", args + [account_id]
    if only_open:
        q += " AND status IN ('DRAFT','ACTIVE','PARTIALLY_FILLED')"
    out = []
    for p in con.execute(q + " ORDER BY submitted_at_utc DESC", args):
        pre = json.loads(p["pretrade_json"] or "{}").get("check", {})
        out.append({"plan_id": p["plan_id"], "status": p["status"], "account_id": p["account_id"],
                    "contract_id": p["contract_id"], "side": p["side"], "quantity": p["quantity"],
                    "filled_quantity": p["filled_quantity"], "entry_ticks": p["entry_ticks"],
                    "stop_ticks": p["stop_ticks"], "target_ticks": p["target_ticks"],
                    "conclusion": pre.get("conclusion"), "submitted_at_utc": sessions.iso(p["submitted_at_utc"]),
                    "expires_at_utc": sessions.iso(p["expires_at_utc"])})
    return out
