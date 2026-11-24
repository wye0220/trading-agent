"""运维状态：运行锁、水位、心跳、覆盖值。都在运行库里，进程重启后照样可读。"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time


def get(con: sqlite3.Connection, key: str, default=None):
    r = con.execute("SELECT value_json FROM ops_state WHERE key=?", (key,)).fetchone()
    return default if r is None else json.loads(r[0])


def put(con: sqlite3.Connection, key: str, value, now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    with con:
        con.execute("INSERT INTO ops_state VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
                    " updated_at_utc=excluded.updated_at_utc", (key, json.dumps(value, ensure_ascii=False), now))


def heartbeat(con: sqlite3.Connection, name: str, now: int, **detail) -> None:
    put(con, f"heartbeat:{name}", {"at_utc": now, **detail}, now)


def all_state(con: sqlite3.Connection) -> dict:
    return {k: {"value": json.loads(v), "updated_at_utc": t} for k, v, t in
            con.execute("SELECT key, value_json, updated_at_utc FROM ops_state ORDER BY key")}


def holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def acquire(con: sqlite3.Connection, name: str, ttl: int, now: int | None = None, who: str | None = None) -> bool:
    """拿锁。锁被别人持有且未过期时返回 False；过期的锁直接接管。"""
    now = int(time.time()) if now is None else now
    who = who or holder()
    con.execute("BEGIN IMMEDIATE")
    try:
        r = con.execute("SELECT holder, expires_at_utc FROM ops_locks WHERE name=?", (name,)).fetchone()
        if r is not None and r[1] > now and r[0] != who:
            con.rollback()
            return False
        con.execute("INSERT INTO ops_locks VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET holder=excluded.holder,"
                    " acquired_at_utc=excluded.acquired_at_utc, expires_at_utc=excluded.expires_at_utc",
                    (name, who, now, now + ttl))
        con.commit()
        return True
    except BaseException:
        con.rollback()
        raise


def release(con: sqlite3.Connection, name: str, who: str | None = None) -> None:
    with con:
        con.execute("DELETE FROM ops_locks WHERE name=? AND holder=?", (name, who or holder()))


def locks(con: sqlite3.Connection, now: int) -> list[dict]:
    return [{"name": n, "holder": h, "acquired_at_utc": a, "expires_at_utc": e, "expired": e <= now}
            for n, h, a, e in con.execute("SELECT * FROM ops_locks ORDER BY name")]


def alert(con: sqlite3.Connection, level: str, kind: str, message: str, *, scope: str | None = None,
          detail: dict | None = None, now: int | None = None) -> None:
    with con:
        con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, created_at_utc) VALUES (?,?,?,?,?,?)",
                    (level, kind, scope, message, json.dumps(detail or {}, ensure_ascii=False),
                     int(time.time()) if now is None else now))
