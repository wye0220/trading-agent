"""评测窗口：从基线登记的封存胶囊里分配盲筛窗和确认窗，记解封账本。按品种和实际日期记录。"""
from __future__ import annotations

import json
import sqlite3

SCREEN, CONFIRM = "SCREEN", "CONFIRM"


class WindowError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def capsules(con: sqlite3.Connection, registry_id: str) -> list[dict]:
    return [{"seq": r["seq"], "start_day": r["start_day"], "end_day": r["end_day"], "valid_days": r["valid_days"],
             "days": json.loads(r["days_json"]), "content_sha256": r["content_sha256"]}
            for r in con.execute("SELECT * FROM data_segments WHERE registry_id=? AND kind='CAPSULE' ORDER BY seq",
                                 (registry_id,))]


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start <= b_end and b_start <= a_end


def state(con: sqlite3.Connection, root: str, caps: list[dict]) -> dict[int, str]:
    """每个胶囊现在的状态：FREE、ASSIGNED 或 UNSEALED。按实际日期判断，换了登记名也认得出同一段日子。"""
    rows = con.execute("SELECT start_day, end_day, event, registration_id, purpose FROM window_ledger"
                       " WHERE instrument_root=? ORDER BY entry_id", (root,)).fetchall()
    out = {}
    for c in caps:
        st, holders = "FREE", set()
        for r in rows:
            if not _overlaps(c["start_day"], c["end_day"], r["start_day"], r["end_day"]):
                continue
            key = (r["registration_id"], r["purpose"])
            if r["event"] == "UNSEALED":
                st = "UNSEALED"
            elif r["event"] == "ASSIGNED":
                holders.add(key)
            elif r["event"] == "RETURNED":
                holders.discard(key)
        out[c["seq"]] = st if st == "UNSEALED" else ("ASSIGNED" if holders else "FREE")
    return out


def allocate(con: sqlite3.Connection, root: str, registry_id: str, n_screen: int, n_confirm: int) -> dict:
    """按日期顺序取一段连续的空闲胶囊：前 n_screen 个盲筛，紧接的 n_confirm 个确认。"""
    caps = capsules(con, registry_id)
    st = state(con, root, caps)
    need = n_screen + n_confirm
    for i in range(len(caps) - need + 1):
        run = caps[i:i + need]
        if all(st[c["seq"]] == "FREE" for c in run) and all(b["seq"] == a["seq"] + 1 for a, b in zip(run, run[1:])):
            return {"screen": run[:n_screen], "confirm": run[n_screen:]}
    free = sum(v == "FREE" for v in st.values())
    raise WindowError("NO_WINDOW", f"{root} 没有连续 {need} 个未用过的胶囊（共 {len(caps)} 个，空闲 {free} 个）")


def window_of(caps: list[dict]) -> dict:
    days = [d for c in caps for d in c["days"]]
    return {"capsules": [c["seq"] for c in caps], "start_day": caps[0]["start_day"], "end_day": caps[-1]["end_day"],
            "valid_days": [d[0] for d in days if d[1]], "all_days": [d[0] for d in days],
            "content": {str(c["seq"]): c["content_sha256"] for c in caps}}


def record(con: sqlite3.Connection, registry_id: str, root: str, caps: list[dict], purpose: str, event: str,
           registration_id: str, candidate_id: str, now: int, detail: dict | None = None) -> None:
    for c in caps:
        con.execute("INSERT INTO window_ledger (registry_id, instrument_root, capsule_seq, start_day, end_day, purpose,"
                    " event, registration_id, candidate_id, detail_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (registry_id, root, c["seq"], c["start_day"], c["end_day"], purpose, event, registration_id,
                     candidate_id, json.dumps(detail or {}, ensure_ascii=False), now))


def events(con: sqlite3.Connection, registration_id: str, purpose: str) -> set[str]:
    return {r[0] for r in con.execute("SELECT event FROM window_ledger WHERE registration_id=? AND purpose=?",
                                      (registration_id, purpose))}


def unsealed_by_anyone(con: sqlite3.Connection, root: str, start_day: str, end_day: str) -> bool:
    return con.execute("SELECT 1 FROM window_ledger WHERE instrument_root=? AND event='UNSEALED' AND start_day<=?"
                       " AND end_day>=? LIMIT 1", (root, end_day, start_day)).fetchone() is not None
