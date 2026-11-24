"""准备清单、批准、回滚、状态。发布库（pointer.py）是 active 的唯一来源；运行库的 release_versions 是镜像。

批准顺序：发布守卫 → 人工确认 → 按当前状态重算清单、核对摘要 → 发布库一个事务里切换 → 更新镜像和候选状态。
两库之间不是一个事务：发布库提交后、镜像更新前中断时，镜像落后；status() 以发布库为准修正。
"""
from __future__ import annotations

import json
import sqlite3
import time

from ..config import Scope, Settings
from ..engine.config import RiskConfig
from ..evolve import candidates as cands
from . import freeze as rfreeze
from . import manifest as rman
from . import pointer
from .config import ReleaseConfig
from .guard import Approval, ReleaseError, check_approval, verify_release_code


def _event(con, release_id, candidate_id, scope_text, event, from_status, to_status, actor, detail, now):
    con.execute("INSERT INTO release_version_events (release_id, candidate_id, scope, event, from_status, to_status,"
                " actor, detail_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (release_id, candidate_id, scope_text, event, from_status, to_status, actor, pointer.canon(detail), now))


def version_row(con: sqlite3.Connection, release_id: str) -> dict | None:
    r = con.execute("SELECT * FROM release_versions WHERE release_id=?", (release_id,)).fetchone()
    if r is None:
        return None
    d = {k: r[k] for k in r.keys()}
    d["manifest"] = json.loads(d.pop("manifest_json"))
    return d


def _find(con: sqlite3.Connection, scope: Scope, target: str) -> dict | None:
    r = con.execute("SELECT release_id FROM release_versions WHERE scope=? AND (candidate_id=? OR (baseline_id=? AND"
                    " release_type=?)) ORDER BY updated_at_utc DESC, rowid DESC LIMIT 1",
                    (str(scope), target, target, pointer.INITIAL)).fetchone()
    return version_row(con, r[0]) if r else None


def _set_status(con, release_id: str, to: str, actor: str, detail: dict, now: int, **extra) -> None:
    row = version_row(con, release_id)
    if row is None or row["status"] == to:
        return
    sets = ", ".join(f"{k}=?" for k in extra)
    con.execute(f"UPDATE release_versions SET status=?, version=version+1, updated_at_utc=?{', ' + sets if sets else ''}"
                " WHERE release_id=?", (to, now, *extra.values(), release_id))
    _event(con, release_id, row["candidate_id"], row["scope"], "STATUS_CHANGED", row["status"], to, actor, detail, now)


def _build(settings, con, release_con, scope, target, cfg, *, registry, risk_cfg, checks=None, now=None) -> dict:
    """target 是候选标识，或首次基线的 baseline_id（'INITIAL' 表示取该 scope 最新的冻结基线）。"""
    if target.startswith("cand_"):
        return rman.candidate(settings, con, release_con, scope, target, cfg, registry=registry, risk_cfg=risk_cfg, now=now)
    return rman.initial(settings, con, release_con, scope, cfg, registry=registry, risk_cfg=risk_cfg,
                        baseline_id=None if target == "INITIAL" else target, checks=checks)


def prepare(settings: Settings, con: sqlite3.Connection, release_con, scope: Scope, target: str, cfg: ReleaseConfig, *,
            registry, risk_cfg: RiskConfig, checks: dict | None = None, now: int | None = None, out_dir=None) -> dict:
    now = now or int(time.time())
    m = _build(settings, con, release_con, scope, target, cfg, registry=registry, risk_cfg=risk_cfg, checks=checks, now=now)
    h = rman.manifest_hash(m)
    old = version_row(con, m["release_id"])
    with con:
        if old is None:
            con.execute("INSERT INTO release_versions (release_id, scope, release_type, candidate_id, baseline_id,"
                        " previous_release, status, manifest_json, manifest_hash, created_at_utc, updated_at_utc)"
                        " VALUES (?,?,?,?,?,?,'PROPOSED',?,?,?,?)",
                        (m["release_id"], str(scope), m["release_type"], m["candidate_id"], m["baseline_id"],
                         m["previous_release"], pointer.canon(m), h, now, now))
            _event(con, m["release_id"], m["candidate_id"], str(scope), "PREPARED", None, "PROPOSED", None,
                   {"manifest_hash": h}, now)
        elif old["manifest_hash"] != h:
            if old["status"] != "PROPOSED":
                raise ReleaseError("ALREADY_RELEASED", f"{m['release_id']} 的状态是 {old['status']}，不能改清单")
            con.execute("UPDATE release_versions SET manifest_json=?, manifest_hash=?, version=version+1, updated_at_utc=?"
                        " WHERE release_id=?", (pointer.canon(m), h, now, m["release_id"]))
            _event(con, m["release_id"], m["candidate_id"], str(scope), "MANIFEST_REFRESHED", "PROPOSED", "PROPOSED",
                   None, {"from": old["manifest_hash"], "to": h}, now)
    files = rman.write_files(settings, m, h, out_dir)
    return {"status": "PREPARED", "release_id": m["release_id"], "release_type": m["release_type"], "manifest_hash": h,
            "acceptance_passed": rman.acceptance_passed(m), "manifest": m, "files": files}


def approve(settings: Settings, con: sqlite3.Connection, release_con: sqlite3.Connection, scope: Scope, target: str,
            manifest_hash: str, approval: Approval | None, cfg: ReleaseConfig, *, registry, risk_cfg: RiskConfig,
            checks: dict | None = None, guard=verify_release_code, now: int | None = None) -> dict:
    now = now or int(time.time())
    guard(settings)
    approval = check_approval(settings, cfg, approval)
    row = _find(con, scope, target)
    if row is None:
        raise ReleaseError("NOT_PREPARED", f"{target} 还没有准备发布清单：先运行 release --prepare")
    cur = pointer.resolve(release_con, scope)
    if cur and cur["release_id"] == row["release_id"] and cur["manifest_hash"] == manifest_hash:
        _mirror_active(con, scope, row, approval, now, cur)
        return {"status": "ALREADY_ACTIVE", "release_id": row["release_id"], "manifest_hash": manifest_hash}
    if manifest_hash != row["manifest_hash"]:
        raise ReleaseError("MANIFEST_MISMATCH", "给定的 manifest_hash 与准备好的清单不一致")
    m = _build(settings, con, release_con, scope, target, cfg, registry=registry, risk_cfg=risk_cfg, checks=checks, now=now)
    h = rman.manifest_hash(m)
    if h != manifest_hash or m["release_id"] != row["release_id"]:
        raise ReleaseError("MANIFEST_MISMATCH", "按当前状态重算的清单与批准的不一致：候选、基线或代码在准备之后变过")
    if not rman.acceptance_passed(m):
        bad = [k for k, v in m["acceptance"].items() if v.get("status") != "PASS"]
        raise ReleaseError("ACCEPTANCE_NOT_MET", f"首次基线的验收没有全部通过：{'、'.join(bad)}")
    r = pointer.activate(release_con, scope, m, h, approval, now=now)
    if r["status"] == "ALREADY_ACTIVE":
        return r
    _mirror_active(con, scope, row, approval, now, pointer.resolve(release_con, scope), replaced=r["replaced"])
    return {**r, "release_type": m["release_type"]}


def _mirror_active(con, scope: Scope, row: dict, approval: Approval, now: int, cur: dict | None, replaced=None) -> None:
    with con:
        if replaced:
            _set_status(con, replaced, "SUPERSEDED", approval.actor, {"by": row["release_id"]}, now)
            old = version_row(con, replaced)
            if old and old["candidate_id"]:
                con.execute("UPDATE candidates SET status='SUPERSEDED', version=version+1, updated_at_utc=? WHERE"
                            " candidate_id=?", (now, old["candidate_id"]))
                cands.ledger(con, old["candidate_id"], str(scope), None, "STATUS_CHANGED", from_status="ACTIVE",
                             to_status="SUPERSEDED", detail={"by": row["release_id"]}, now=now)
        _set_status(con, row["release_id"], "APPROVED", approval.actor, {"channel": approval.channel}, now)
        _set_status(con, row["release_id"], "ACTIVE", approval.actor, {}, now, activated_at_utc=now)
    if row["candidate_id"]:
        for st in ("APPROVED", "ACTIVE"):
            cands.set_status(con, row["candidate_id"], st, f"RELEASE:{row['release_id']}", now=now)
    # 新的批准解除 RELEASE 冻结（首次基线被停用入场之后再发布的情形）
    for x in rfreeze.active_causes(con, scope):
        if x["cause"] == "RELEASE":
            from ..engine import runner
            runner.set_freeze(con, scope, x["risk_domain"], frozen=False, cause="RELEASE",
                              reason=f"新版本 {row['release_id']} 已批准", actor=approval.actor)


def rollback(settings: Settings, con: sqlite3.Connection, release_con: sqlite3.Connection, scope: Scope,
             approval: Approval | None, cfg: ReleaseConfig, reason: str, *, guard=verify_release_code,
             now: int | None = None) -> dict:
    now = now or int(time.time())
    guard(settings)
    approval = check_approval(settings, cfg, approval)
    cur = pointer.resolve(release_con, scope)
    if cur is None:
        raise ReleaseError("NO_ACTIVE", f"{scope} 没有 active 版本，无从回滚")
    if not (cur["manifest"] or {}).get("previous_release"):
        # 没有上一版本：先冻结（引擎提交订单前会重读），再停用指针的入场权限
        rfreeze.freeze(con, scope, "RELEASE", f"回滚首次基线：{reason}", approval.actor)
    r = pointer.rollback(release_con, scope, approval, reason, now=now)
    if r["status"] == "ALREADY_DISABLED":
        return r
    with con:
        _set_status(con, r["from_release"], "ROLLED_BACK", approval.actor, {"reason": reason}, now)
        if r["to_release"]:
            _set_status(con, r["to_release"], "ACTIVE", approval.actor, {"reason": "ROLLBACK_TARGET"}, now,
                        activated_at_utc=now)
    frm = version_row(con, r["from_release"])
    if frm and frm["candidate_id"]:
        cands.set_status(con, frm["candidate_id"], "ROLLED_BACK", f"ROLLBACK:{reason}", now=now)
    to = version_row(con, r["to_release"]) if r["to_release"] else None
    if to and to["candidate_id"]:
        cands.set_status(con, to["candidate_id"], "ACTIVE", "ROLLBACK_TARGET", now=now)
    return r


def status(con: sqlite3.Connection, release_con, scope: Scope) -> dict:
    """以发布库为准；镜像落后时修正（只改运行库的镜像，不写发布库）。"""
    cur = pointer.resolve(release_con, scope)
    fixed = []
    if cur is not None:
        row = version_row(con, cur["release_id"])
        if row is not None and row["status"] != "ACTIVE":
            with con:
                _set_status(con, cur["release_id"], "ACTIVE", "status", {"reason": "SYNC_FROM_RELEASE_DB"}, int(time.time()))
            fixed.append(cur["release_id"])
    pending = [dict(release_id=r[0], release_type=r[1], target=r[2] or r[3], manifest_hash=r[4]) for r in con.execute(
        "SELECT release_id, release_type, candidate_id, baseline_id, manifest_hash FROM release_versions WHERE scope=?"
        " AND status='PROPOSED' ORDER BY updated_at_utc DESC", (str(scope),))]
    shadows = [dict(shadow_id=r[0], candidate_id=r[1], start_day=json.loads(r[2])["start_day"],
                    valid_days=json.loads(r[2])["valid_days"], result=r[3]) for r in con.execute(
        "SELECT s.shadow_id, s.candidate_id, s.plan_json, x.status FROM shadow_registrations s LEFT JOIN shadow_results x"
        " ON x.shadow_id=s.shadow_id WHERE s.scope=? ORDER BY s.created_at_utc DESC", (str(scope),))]
    return {"scope": str(scope), "active": None if cur is None else {
                "release_id": cur["release_id"], "manifest_hash": cur["manifest_hash"],
                "entries_enabled": cur["entries_enabled"],
                "release_type": (cur["manifest"] or {}).get("release_type"),
                "labels": (cur["manifest"] or {}).get("labels") or []},
            "freeze": rfreeze.status(con, scope), "pending": pending, "shadows": shadows, "mirror_fixed": fixed,
            "events": pointer.events(release_con, str(scope), 10) if release_con is not None else []}
