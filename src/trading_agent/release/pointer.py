"""发布库的读写。整个项目里只有这个文件写发布库（active 指针、已批准清单、审计事件）。

写操作都在一个 BEGIN IMMEDIATE 事务里：核验当前 active、写清单、改指针、追加事件，一起提交或一起放弃。
盘中进程只用 resolve 读，并且在调用方开好的读事务里读，拿到的是一个完整的已提交版本。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .. import db
from ..config import Scope
from .guard import Approval, ReleaseError

INITIAL = "INITIAL_PAPER_BASELINE"
CANDIDATE = "CANDIDATE"


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _key(scope: Scope) -> tuple:
    return (scope.strategy_family, scope.instrument_root, scope.session_type, scope.timeframe)


def open_readonly(path: Path | str) -> sqlite3.Connection | None:
    """只读打开发布库，不迁移。文件不存在时返回 None。"""
    p = Path(path)
    if not p.is_file():
        return None
    con = db.connect(p, readonly=True)
    con.isolation_level = None
    return con


def has_manifests(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_manifests'").fetchone() is not None


def resolve(con: sqlite3.Connection | None, scope: Scope) -> dict | None:
    """该 scope 当前的 active 版本：指针加已批准清单。没有指针时返回 None。"""
    if con is None:
        return None
    r = con.execute("SELECT release_id, manifest_hash, entries_enabled, version, updated_at_utc FROM active_pointers"
                    " WHERE strategy_family=? AND instrument_root=? AND session_type=? AND timeframe=?", _key(scope)).fetchone()
    if r is None:
        return None
    man = None
    if has_manifests(con):
        m = con.execute("SELECT manifest_json FROM release_manifests WHERE release_id=?", (r[0],)).fetchone()
        man = json.loads(m[0]) if m else None
    return {"release_id": r[0], "manifest_hash": r[1], "entries_enabled": bool(r[2]), "pointer_version": r[3],
            "updated_at_utc": r[4], "manifest": man}


def manifest_of(con: sqlite3.Connection, release_id: str) -> dict | None:
    if not has_manifests(con):
        return None
    m = con.execute("SELECT manifest_json, manifest_hash, approved_by, approval_channel, approved_at_utc"
                    " FROM release_manifests WHERE release_id=?", (release_id,)).fetchone()
    if m is None:
        return None
    return {"manifest": json.loads(m[0]), "manifest_hash": m[1], "approved_by": m[2], "approval_channel": m[3],
            "approved_at_utc": m[4]}


def events(con: sqlite3.Connection, scope_text: str | None = None, limit: int = 20) -> list[dict]:
    q = "SELECT * FROM release_events" + (" WHERE scope=?" if scope_text else "") + " ORDER BY event_id DESC LIMIT ?"
    rows = con.execute(q, ((scope_text, limit) if scope_text else (limit,))).fetchall()
    cols = [d[0] for d in con.execute("SELECT * FROM release_events LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


def _event(con, scope_text, event_type, release_id, previous, manifest_hash, actor, detail, now):
    con.execute("INSERT INTO release_events (scope, event_type, release_id, previous_release, manifest_hash, actor,"
                " detail_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                (scope_text, event_type, release_id, previous, manifest_hash, actor, canon(detail), now))


def _begin(con: sqlite3.Connection):
    if con.in_transaction:
        raise RuntimeError("发布库的写操作需要自己开事务")
    con.execute("BEGIN IMMEDIATE")


def activate(con: sqlite3.Connection, scope: Scope, manifest: dict, manifest_hash: str, approval: Approval, *,
             now: int | None = None) -> dict:
    """批准并切换：一个事务里核验当前 active、写清单、改指针、记事件。重复批准同一清单是空操作。"""
    now = now or int(time.time())
    rid, rtype, prev = manifest["release_id"], manifest["release_type"], manifest.get("previous_release")
    _begin(con)
    try:
        cur = resolve(con, scope)
        if cur and cur["release_id"] == rid and cur["manifest_hash"] == manifest_hash:
            con.rollback()
            return {"status": "ALREADY_ACTIVE", "release_id": rid, "manifest_hash": manifest_hash}
        if con.execute("SELECT 1 FROM release_manifests WHERE release_id=?", (rid,)).fetchone():
            raise ReleaseError("ALREADY_RELEASED", f"{rid} 批准过，现在不是 active；要重新发布请重新准备清单")
        if rtype == INITIAL:
            if cur is not None:
                raise ReleaseError("ACTIVE_EXISTS", f"{scope} 已有 active 版本 {cur['release_id']}，首次基线不能覆盖它")
            if prev is not None:
                raise ReleaseError("MANIFEST_INVALID", "首次基线的 previous_release 必须为空")
        else:
            if cur is None:
                raise ReleaseError("NO_ACTIVE", f"{scope} 还没有 active 版本：先建立首次纸面基线")
            if cur["release_id"] != prev:
                raise ReleaseError("STALE_BASE", f"清单基于 {prev}，而当前 active 是 {cur['release_id']}")
        con.execute("INSERT INTO release_manifests VALUES (?,?,?,?,?,?,?,?,?)",
                    (rid, str(scope), rtype, prev, manifest_hash, canon(manifest), approval.actor, approval.channel, now))
        if cur is None:
            con.execute("INSERT INTO active_pointers (strategy_family, instrument_root, session_type, timeframe,"
                        " release_id, manifest_hash, entries_enabled, version, updated_at_utc) VALUES (?,?,?,?,?,?,1,1,?)",
                        (*_key(scope), rid, manifest_hash, now))
        else:
            c = con.execute("UPDATE active_pointers SET release_id=?, manifest_hash=?, entries_enabled=1,"
                            " version=version+1, updated_at_utc=? WHERE strategy_family=? AND instrument_root=?"
                            " AND session_type=? AND timeframe=? AND version=?",
                            (rid, manifest_hash, now, *_key(scope), cur["pointer_version"]))
            if c.rowcount != 1:
                raise ReleaseError("STALE_POINTER", "active 指针在此期间被改动过")
        _event(con, str(scope), "APPROVED", rid, prev, manifest_hash, approval.actor,
               {"channel": approval.channel, "release_type": rtype}, now)
        _event(con, str(scope), "ACTIVATED", rid, cur["release_id"] if cur else None, manifest_hash, approval.actor, {}, now)
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return {"status": "ACTIVATED", "release_id": rid, "manifest_hash": manifest_hash,
            "replaced": cur["release_id"] if cur else None}


def rollback(con: sqlite3.Connection, scope: Scope, approval: Approval, reason: str, *, now: int | None = None) -> dict:
    """指回清单登记的上一已批准版本；没有上一版本时停用入场权限（指针和记录保留）。"""
    now = now or int(time.time())
    _begin(con)
    try:
        cur = resolve(con, scope)
        if cur is None:
            raise ReleaseError("NO_ACTIVE", f"{scope} 没有 active 版本，无从回滚")
        prev = (cur["manifest"] or {}).get("previous_release")
        if prev:
            tgt = con.execute("SELECT manifest_hash FROM release_manifests WHERE release_id=?", (prev,)).fetchone()
            if tgt is None:
                raise ReleaseError("NO_ROLLBACK_TARGET", f"回滚目标 {prev} 不在已批准清单里")
            con.execute("UPDATE active_pointers SET release_id=?, manifest_hash=?, entries_enabled=1, version=version+1,"
                        " updated_at_utc=? WHERE strategy_family=? AND instrument_root=? AND session_type=? AND timeframe=?",
                        (prev, tgt[0], now, *_key(scope)))
            _event(con, str(scope), "ROLLED_BACK", prev, cur["release_id"], tgt[0], approval.actor,
                   {"reason": reason, "channel": approval.channel}, now)
            out = {"status": "ROLLED_BACK", "from_release": cur["release_id"], "to_release": prev}
        else:
            if not cur["entries_enabled"]:
                con.rollback()
                return {"status": "ALREADY_DISABLED", "release_id": cur["release_id"]}
            con.execute("UPDATE active_pointers SET entries_enabled=0, version=version+1, updated_at_utc=?"
                        " WHERE strategy_family=? AND instrument_root=? AND session_type=? AND timeframe=?",
                        (now, *_key(scope)))
            _event(con, str(scope), "ENTRIES_DISABLED", cur["release_id"], None, cur["manifest_hash"], approval.actor,
                   {"reason": reason, "channel": approval.channel, "note": "没有上一已批准版本"}, now)
            out = {"status": "ENTRIES_DISABLED", "from_release": cur["release_id"], "to_release": None}
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    return out
