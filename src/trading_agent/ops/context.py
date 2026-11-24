"""当日约束（控制通道的 context）。只能收紧风险：禁止新入场、降低手数上限。"""
from __future__ import annotations

import sqlite3


def active(con: sqlite3.Connection, scope_text: str, now: int) -> list[dict]:
    """此刻对该 scope 生效的约束（含 scope=ALL）。表不存在时没有约束。"""
    try:
        rows = con.execute("SELECT constraint_id, kind, scope, value_json, until_utc FROM context_constraints"
                           " WHERE (scope=? OR scope='ALL') AND active_from_utc<=? AND until_utc>? ORDER BY created_at_utc",
                           (scope_text, now, now)).fetchall()
    except sqlite3.OperationalError:
        return []
    import json
    return [{"constraint_id": r[0], "kind": r[1], "scope": r[2], "value": json.loads(r[3]), "until_utc": r[4]} for r in rows]


def no_entry(con: sqlite3.Connection, scope_text: str, now: int) -> dict | None:
    return next((c for c in active(con, scope_text, now) if c["kind"] == "NO_ENTRY"), None)


def max_qty(con: sqlite3.Connection, scope_text: str, now: int) -> int | None:
    caps = [int(c["value"]["max_qty"]) for c in active(con, scope_text, now) if c["kind"] == "MAX_QTY"]
    return min(caps) if caps else None
