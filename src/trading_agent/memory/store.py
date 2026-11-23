"""Memory 的存储与生命周期：候选 → 确认 →（评测）→ 条目；快照、头指针、回滚。所有变动都记 memory_events。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Mapping

from .. import db
from ..config import Settings
from . import items as I
from .registry import Registry, load_registry

USER_CLI = "user:cli"
PENDING, PENDING_EVAL, CONFIRMED, REJECTED = "PENDING_CONFIRMATION", "PENDING_EVALUATION", "CONFIRMED", "REJECTED"
REJECT_REASONS = ("NOT_WANTED", "WRONG", "DUPLICATE", "INVALID", "EVALUATION_FAILED")


class MemoryError_(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def version_id(members: list[tuple[str, int]]) -> str:
    return "mem_" + hashlib.sha256(I.canon(sorted(members)).encode()).hexdigest()[:16]


def head(con: sqlite3.Connection) -> dict | None:
    r = con.execute("SELECT memory_version, version FROM memory_head WHERE head_key='global'").fetchone()
    return None if r is None else {"memory_version": r[0], "version": r[1]}


def members(con: sqlite3.Connection, memory_version: str | None) -> list[tuple[str, int]]:
    if memory_version is None:
        return []
    r = con.execute("SELECT members_json FROM memory_versions WHERE memory_version=?", (memory_version,)).fetchone()
    if r is None:
        raise MemoryError_("UNKNOWN_VERSION", f"没有这个 memory_version：{memory_version}")
    return [(a, b) for a, b in json.loads(r[0])]


def _event(con, event, actor, now, *, cand=None, item=None, item_version=None, frm=None, to=None, detail=None):
    con.execute("INSERT INTO memory_events (event, memory_candidate_id, item_id, item_version, from_memory_version,"
                " to_memory_version, actor, detail_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (event, cand, item, item_version, frm, to, actor, I.canon(detail or {}), now))


def _begin(con):
    if con.in_transaction:
        raise RuntimeError("Memory 的写操作需要自己开事务；调用前请先提交或回滚")
    con.execute("BEGIN IMMEDIATE")          # 先拿写锁再读头指针：确认和回滚之间不会互相覆盖


def _move_head(con, new_members: list[tuple[str, int]], now: int, expected: dict | None) -> tuple[str | None, str]:
    """写入快照并移动头指针。expected 是事务开始后读到的头；与库中不一致时抛 StaleVersion。"""
    mv = version_id(new_members)
    con.execute("INSERT OR IGNORE INTO memory_versions VALUES (?,?,?,?)",
                (mv, I.canon(sorted(new_members)), len(new_members), now))
    if expected is None:
        try:
            con.execute("INSERT INTO memory_head VALUES ('global',?,1,?)", (mv, now))
        except sqlite3.IntegrityError:
            raise db.StaleVersion("Memory 头指针已被别的写入建立") from None
        _sync_status(con, new_members)
        return None, mv
    db.versioned_update(con, "memory_head", "head_key", "global", expected["version"], memory_version=mv,
                        updated_at_utc=now)
    _sync_status(con, new_members)
    return expected["memory_version"], mv


def _sync_status(con, mem: list[tuple[str, int]]) -> None:
    """让条目的状态列跟上头指针：成员是 ACTIVE，曾经生效而不在成员里的是 SUPERSEDED。EXPIRED 不动。"""
    keep = set(mem)
    for r in con.execute("SELECT item_id, version, status FROM memory_items WHERE status IN ('ACTIVE','SUPERSEDED')").fetchall():
        want = "ACTIVE" if (r[0], r[1]) in keep else "SUPERSEDED"
        if r[2] != want:
            con.execute("UPDATE memory_items SET status=? WHERE item_id=? AND version=?", (want, r[0], r[1]))


def candidate(con: sqlite3.Connection, candidate_id: str) -> dict | None:
    r = con.execute("SELECT * FROM memory_candidates WHERE memory_candidate_id=?", (candidate_id,)).fetchone()
    if r is None:
        return None
    return {"memory_candidate_id": r["memory_candidate_id"], "origin": r["session_id"], "note_id": r["note_id"],
            "status": r["status"], "created_at": I.iso(r["created_at_utc"]), "body": json.loads(r["body_json"])}


def candidates(con: sqlite3.Connection, statuses=(PENDING, PENDING_EVAL)) -> list[dict]:
    q = ",".join("?" * len(statuses))
    return [candidate(con, r[0]) for r in con.execute(
        f"SELECT memory_candidate_id FROM memory_candidates WHERE status IN ({q}) ORDER BY created_at_utc,"
        " memory_candidate_id", statuses)]


def propose(settings: Settings, con: sqlite3.Connection, body: Mapping, *, origin: str, note_id: str | None = None,
            reg: Registry | None = None, now: int | None = None) -> dict:
    """登记一条候选（来自 6A／6B）。先校验；同一来源、同一内容不重复登记。"""
    reg = reg or load_registry(settings.project_root)
    now = int(time.time()) if now is None else now
    ok = I.validate(settings, reg, body, now_utc=now)
    cid = "memc_" + hashlib.sha256(f"{note_id or origin}|{I.canon(ok)}".encode()).hexdigest()[:20]
    with con:
        cur = con.execute("INSERT OR IGNORE INTO memory_candidates VALUES (?,?,?,?,?,?)",
                          (cid, origin, note_id, I.canon(ok), PENDING, now))
        if cur.rowcount:
            _event(con, "PROPOSED", origin if origin.startswith("user:") else "system", now, cand=cid,
                   detail={"origin": origin})
    return {"memory_candidate_id": cid, "created": bool(cur.rowcount)}


def _latest(con, iid: str):
    return con.execute("SELECT * FROM memory_items WHERE item_id=? ORDER BY version DESC LIMIT 1", (iid,)).fetchone()


def _activate(settings, reg, con, cand: dict, body: dict, actor: str, now: int) -> dict:
    """在已开的事务里把候选写成条目并移动头指针。"""
    iid = I.item_id(body)
    prev = _latest(con, iid)
    cur_head = head(con)
    mem = members(con, cur_head["memory_version"]) if cur_head else []
    live = next((v for i, v in mem if i == iid), None)
    if live is not None:
        row = con.execute("SELECT statement_json, valid_until_utc FROM memory_items WHERE item_id=? AND version=?",
                          (iid, live)).fetchone()
        until_new = I.parse_iso(body["valid_until"]) if body["valid_until"] else None
        if row["statement_json"] == I.canon(body["statement"]) and row["valid_until_utc"] == until_new:
            raise MemoryError_("NO_CHANGE", f"已经有内容相同的生效条目 {iid}")
    ver = (prev["version"] + 1) if prev is not None else 1
    until = I.parse_iso(body["valid_until"]) if body["valid_until"] else None
    item = {"item_id": iid, **body, "confirmed_by": actor, "created_at": I.iso(now), "status": "ACTIVE", "version": ver}
    errs = I.schema_errors(settings.project_root, "MemoryItem", item)
    if errs:
        raise MemoryError_("SCHEMA_INVALID", "；".join(errs[:3]))
    con.execute("INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                (iid, ver, body["kind"], body["scope"], I.canon(body["statement"]), I.canon(body["source_event_ids"]),
                 actor, "ACTIVE", until, now))
    frm, to = _move_head(con, [m for m in mem if m[0] != iid] + [(iid, ver)], now, cur_head)
    con.execute("UPDATE memory_candidates SET status=? WHERE memory_candidate_id=?", (CONFIRMED, cand["memory_candidate_id"]))
    _event(con, "ACTIVATED", actor, now, cand=cand["memory_candidate_id"], item=iid, item_version=ver, frm=frm, to=to,
           detail={"replaced_version": prev["version"] if prev is not None else None})
    return {"item_id": iid, "version": ver, "memory_version": to, "previous_memory_version": frm,
            "sentence": I.sentence(settings, reg, item), "status": CONFIRMED}


def _pending(con, candidate_id: str, allowed: tuple[str, ...]) -> dict:
    c = candidate(con, candidate_id)
    if c is None:
        raise MemoryError_("UNKNOWN_CANDIDATE", f"没有这个 Memory 候选：{candidate_id}")
    if c["status"] not in allowed:
        raise MemoryError_("NOT_PENDING", f"候选 {candidate_id} 的状态是 {c['status']}，不能做这个操作")
    return c


def _run(con, fn):
    _begin(con)
    try:
        out = fn()
        con.commit()
        return out
    except BaseException:
        con.rollback()
        raise


def confirm(settings: Settings, con: sqlite3.Connection, candidate_id: str, *, actor: str = USER_CLI,
            reg: Registry | None = None, now: int | None = None) -> dict:
    """你的确认。偏好立即生效；市场事实和执行模式转 PENDING_EVALUATION，等阶段 8 的评测。"""
    reg = reg or load_registry(settings.project_root)
    now = int(time.time()) if now is None else now
    if not actor.startswith("user:"):
        raise MemoryError_("ACTOR_INVALID", "确认只能来自用户")

    def work():
        c = _pending(con, candidate_id, (PENDING,))
        try:
            body = I.validate(settings, reg, c["body"], now_utc=now)
        except I.Invalid as e:
            raise MemoryError_(e.reason, str(e)) from None
        if body["kind"] == "user_preference":
            return _activate(settings, reg, con, c, body, actor, now)
        con.execute("UPDATE memory_candidates SET status=? WHERE memory_candidate_id=?", (PENDING_EVAL, candidate_id))
        _event(con, "PENDING_EVALUATION", actor, now, cand=candidate_id)
        h = head(con)
        return {"item_id": None, "status": PENDING_EVAL, "memory_version": h["memory_version"] if h else None,
                "sentence": I.sentence(settings, reg, body)}
    return _run(con, work)


def activate_evaluated(settings: Settings, con: sqlite3.Connection, candidate_id: str, evaluation_id: str, *,
                       reg: Registry | None = None, now: int | None = None) -> dict:
    """阶段 8 调用：评测通过后让市场事实或执行模式生效。没有通过的评测记录就拒绝。"""
    reg = reg or load_registry(settings.project_root)
    now = int(time.time()) if now is None else now

    def work():
        c = _pending(con, candidate_id, (PENDING_EVAL,))
        r = con.execute("SELECT status FROM evaluation_runs WHERE evaluation_id=? AND candidate_id=?",
                        (evaluation_id, candidate_id)).fetchone()
        if r is None or r[0] != "PASSED":
            raise MemoryError_("EVALUATION_REQUIRED", f"候选 {candidate_id} 没有通过的评测记录 {evaluation_id}")
        try:
            body = I.validate(settings, reg, c["body"], now_utc=now)
        except I.Invalid as e:
            raise MemoryError_(e.reason, str(e)) from None
        return _activate(settings, reg, con, c, body, f"evaluation:{evaluation_id}", now)
    return _run(con, work)


def reject(con: sqlite3.Connection, candidate_id: str, *, reason: str = "NOT_WANTED", actor: str = USER_CLI,
           now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    if reason not in REJECT_REASONS:
        raise MemoryError_("REASON_INVALID", f"拒绝原因只能是 {'、'.join(REJECT_REASONS)}")

    def work():
        _pending(con, candidate_id, (PENDING, PENDING_EVAL))
        con.execute("UPDATE memory_candidates SET status=? WHERE memory_candidate_id=?", (REJECTED, candidate_id))
        _event(con, "REJECTED", actor, now, cand=candidate_id, detail={"reason": reason})
        return {"memory_candidate_id": candidate_id, "status": REJECTED}
    return _run(con, work)


def supersede(settings: Settings, con: sqlite3.Connection, iid: str, *, evidence: list[str] | tuple = (),
              actor: str = USER_CLI, now: int | None = None) -> dict:
    """条目被新证据否定（或偏好被你撤回）：标 SUPERSEDED，新快照不含它，旧行保留。"""
    now = int(time.time()) if now is None else now
    ev = list(evidence)
    errs = I.schema_errors(settings.project_root, "MemoryCandidate", {
        "kind": "user_preference", "scope": "global", "valid_until": None, "source_event_ids": ev,
        "statement": {"subject": "user_preference", "condition": None, "metric": "x", "value": 1, "unit": None,
                      "sample_n": None, "period": None, "source": "trader_note"}})
    if errs:
        raise MemoryError_("SCHEMA_INVALID", "证据 id 的格式不对")

    def work():
        cur_head = head(con)
        mem = members(con, cur_head["memory_version"]) if cur_head else []
        live = next((v for i, v in mem if i == iid), None)
        if live is None:
            raise MemoryError_("NOT_ACTIVE", f"{iid} 不是当前生效的条目")
        row = con.execute("SELECT * FROM memory_items WHERE item_id=? AND version=?", (iid, live)).fetchone()
        if row["kind"] != "user_preference" and not ev:
            raise MemoryError_("EVIDENCE_REQUIRED", "否定市场事实或执行模式需要给出证据 id")
        frm, to = _move_head(con, [m for m in mem if m[0] != iid], now, cur_head)
        _event(con, "SUPERSEDED", actor, now, item=iid, item_version=row["version"], frm=frm, to=to,
               detail={"evidence": ev})
        return {"item_id": iid, "version": row["version"], "memory_version": to, "previous_memory_version": frm}
    return _run(con, work)


def rollback(con: sqlite3.Connection, memory_version: str, *, actor: str = USER_CLI, now: int | None = None) -> dict:
    """把头指针指回一个已有的快照。条目的行不动；加载按快照的成员取。"""
    now = int(time.time()) if now is None else now

    def work():
        cur_head = head(con)
        if cur_head is None:
            raise MemoryError_("UNKNOWN_VERSION", "还没有任何 memory_version")
        target = members(con, memory_version)
        if memory_version == cur_head["memory_version"]:
            raise MemoryError_("NO_CHANGE", "头指针已经在这个版本")
        frm, to = _move_head(con, target, now, cur_head)
        _event(con, "ROLLBACK", actor, now, frm=frm, to=to)
        return {"memory_version": to, "previous_memory_version": frm, "items": len(target)}
    return _run(con, work)


def sweep(con: sqlite3.Connection, now: int | None = None) -> int:
    """把过了 valid_until 的生效条目标成 EXPIRED（只影响展示；加载本来就不取过期条目，快照不变）。"""
    now = int(time.time()) if now is None else now
    with con:
        rows = con.execute("SELECT item_id, version FROM memory_items WHERE status='ACTIVE' AND valid_until_utc IS NOT NULL"
                           " AND valid_until_utc <= ?", (now,)).fetchall()
        for r in rows:
            con.execute("UPDATE memory_items SET status='EXPIRED' WHERE item_id=? AND version=?", (r[0], r[1]))
            _event(con, "EXPIRED", "system", now, item=r[0], item_version=r[1])
    return len(rows)


def history(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [{"event_id": r["event_id"], "event": r["event"], "memory_candidate_id": r["memory_candidate_id"],
             "item_id": r["item_id"], "item_version": r["item_version"], "from": r["from_memory_version"],
             "to": r["to_memory_version"], "actor": r["actor"], "detail": json.loads(r["detail_json"]),
             "at": I.iso(r["created_at_utc"])}
            for r in con.execute("SELECT * FROM memory_events ORDER BY event_id DESC LIMIT ?", (limit,))]
