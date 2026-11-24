"""冻结与解除。冻结记在运行库的 freeze_events（引擎在挂单前、写锁之内重读它）。

作用域：
- MANUAL：整个 scope（risk_domain='*'），影子臂也停。
- MONITOR、RELEASE：只停常驻纸面运行的风险域，不挡正在进行的影子。
- DAILY_LOSS：指定的风险域，下一个交易日自动解除。
- DATA：由数据校验维护（scope_data_status），数据恢复后自动解除；这里只报告。
"""
from __future__ import annotations

import sqlite3
import time

from ..config import Scope
from ..engine import runner
from ..manual.common import paper_run_id
from ..scan import _session_for
from .config import FREEZE_CAUSES, ReleaseConfig
from .guard import Approval, ReleaseError

SCOPE_WIDE = "*"


def paper_domain(scope: Scope) -> str:
    return f"paper:{paper_run_id(scope)}"


def domain_for(scope: Scope, cause: str) -> str:
    return SCOPE_WIDE if cause == "MANUAL" else paper_domain(scope)


def active_causes(con: sqlite3.Connection, scope: Scope | str, domain: str | None = None) -> list[dict]:
    """每个（风险域，原因）最近一次是冻结的那些。"""
    q = "SELECT risk_domain, cause, action, reason, actor, created_at_utc FROM freeze_events WHERE scope=?"
    args: list = [str(scope)]
    if domain:
        q += " AND risk_domain IN (?, '*')"
        args.append(domain)
    last: dict = {}
    for r in con.execute(q + " ORDER BY created_at_utc, rowid", args):
        last[(r["risk_domain"], r["cause"])] = dict(r)
    return [v for v in last.values() if v["action"] == "FREEZE"]


def freeze(con: sqlite3.Connection, scope: Scope, cause: str, reason: str | None, actor: str | None, *,
           cancel_working: bool = False, domain: str | None = None, now: int | None = None) -> dict:
    if cause not in FREEZE_CAUSES or cause == "DATA":
        raise ReleaseError("BAD_CAUSE", f"冻结原因只能是 {[c for c in FREEZE_CAUSES if c != 'DATA']}（DATA 由数据校验维护）")
    dom = domain or domain_for(scope, cause)
    already = any(x["cause"] == cause and x["risk_domain"] == dom for x in active_causes(con, scope))
    fid = None if already else runner.set_freeze(con, scope, dom, frozen=True, cause=cause, reason=reason, actor=actor)
    cancelled = withdraw_working(con, scope, dom, now=now) if cancel_working else []
    return {"status": "ALREADY_FROZEN" if already else "FROZEN", "freeze_event_id": fid, "scope": str(scope),
            "cause": cause, "risk_domain": dom, "cancelled_orders": cancelled}


def withdraw_working(con: sqlite3.Connection, scope: Scope, domain: str, *, now: int | None = None) -> list[str]:
    """撤掉未成交的入场单（ARMED、TRIGGERED）。已有仓位不动，照常按止损和截止规则管理。"""
    now = now or int(time.time())
    q = "SELECT order_id FROM paper_orders WHERE scope=? AND status IN ('ARMED','TRIGGERED')"
    args: list = [str(scope)]
    if domain != SCOPE_WIDE:
        q += " AND risk_domain=?"
        args.append(domain)
    con.execute("BEGIN IMMEDIATE")
    try:
        ids = [r[0] for r in con.execute(q, args)]
        for oid in ids:
            con.execute("UPDATE paper_orders SET status='CANCELLED', end_reason='FROZEN', version=version+1,"
                        " updated_at_utc=? WHERE order_id=? AND status IN ('ARMED','TRIGGERED')", (now, oid))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return ids


def unfreeze(con: sqlite3.Connection, scope: Scope, cause: str, reason: str, cfg: ReleaseConfig, *,
             approval: Approval | None = None, domain: str | None = None, policy: bool = False) -> dict:
    """按预登记的政策解除一个原因的冻结。人工类要批准人确认；RELEASE 只能由新的批准解除；DATA 不能手动解除。"""
    if cause == "DATA":
        raise ReleaseError("POLICY", "数据冻结在数据恢复后自动解除，不能手动解除")
    if cause in cfg.freeze["approval_release"] and not policy:
        raise ReleaseError("POLICY", f"{cause} 冻结只能由新的发布批准解除")
    if cause in cfg.freeze["human_release"] and approval is None:
        raise ReleaseError("NO_HUMAN_APPROVAL", f"解除 {cause} 冻结要批准人在终端里确认")
    dom = domain or domain_for(scope, cause)
    if not any(x["cause"] == cause and x["risk_domain"] == dom for x in active_causes(con, scope)):
        return {"status": "NOT_FROZEN", "scope": str(scope), "cause": cause, "risk_domain": dom}
    actor = approval.actor if approval else "policy"
    fid = runner.set_freeze(con, scope, dom, frozen=False, cause=cause, reason=reason, actor=actor)
    return {"status": "UNFROZEN", "freeze_event_id": fid, "scope": str(scope), "cause": cause, "risk_domain": dom}


def apply_policies(con: sqlite3.Connection, scope: Scope, now: int) -> list[dict]:
    """自动解除：DAILY_LOSS 冻结在它所在交易日之后的下一个交易日解除。"""
    today, _ = _session_for(now)
    out = []
    for x in active_causes(con, scope):
        if x["cause"] != "DAILY_LOSS":
            continue
        fday, _ = _session_for(x["created_at_utc"])
        if today > fday:
            fid = runner.set_freeze(con, scope, x["risk_domain"], frozen=False, cause="DAILY_LOSS",
                                    reason=f"下一个交易日自动解除（冻结于 {fday}）", actor="policy")
            out.append({"cause": "DAILY_LOSS", "risk_domain": x["risk_domain"], "freeze_event_id": fid})
    return out


def status(con: sqlite3.Connection, scope: Scope) -> dict:
    causes = active_causes(con, scope)
    data = [dict(r) for r in con.execute("SELECT risk_domain, reason, updated_at_utc FROM scope_data_status"
                                         " WHERE scope=? AND blocked=1", (str(scope),))]
    return {"scope": str(scope), "frozen": bool(causes or data), "causes": causes, "data_blocked": data,
            "paper_domain": paper_domain(scope),
            "paper_frozen_reason": runner.frozen_reason(con, scope, paper_domain(scope))}
