"""数据切分登记：开发段、向前验证段、截止日之后的 20 日胶囊。

截止日之后的 K 线不在运行库里。它们的有效性和内容摘要来自导入时留下的 withheld_coverage，
所以登记胶囊不需要、也不会读取任何截止日之后的价格。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date

from .. import contenthash, contracts, sessions
from ..config import Settings
from ..days import pick_source
from .config import ResearchConfig

DEVELOPMENT, FORWARD, CAPSULE, REMAINDER = "DEVELOPMENT", "FORWARD_VALIDATION", "CAPSULE", "REMAINDER"


class SplitError(RuntimeError):
    pass


def _degraded(con, source: str | None, day: str) -> bool:
    if not source:
        return False
    r = con.execute("SELECT condition FROM day_quality WHERE source=? AND day=?", (source, day)).fetchone()
    return bool(r) and r[0] != "available"


def _db_day_hash(con, source: str, cid: str, day: str) -> str:
    acc = 0
    for r in con.execute(
            "SELECT timeframe, bar_start_utc, open, high, low, close, volume FROM bars b WHERE source=? AND contract_id=?"
            " AND trading_day=? AND derived=0 AND revision=(SELECT MAX(revision) FROM bars x WHERE x.source=b.source"
            " AND x.contract_id=b.contract_id AND x.timeframe=b.timeframe AND x.bar_start_utc=b.bar_start_utc)",
            (source, cid, day)):
        acc = contenthash.add(acc, contenthash.bar_digest(source, cid, r[0], r[1], r[2], r[3], r[4], r[5], r[6]))
    return contenthash.to_hex(acc)


def visible_days(con: sqlite3.Connection, root: str) -> list[dict]:
    """运行库里的交易日。有效 = RTH 完整且来源质量未降级。"""
    out = []
    for r in con.execute("SELECT trading_day, contract_id, source, complete, degraded, reason, fixture FROM trading_days"
                         " WHERE instrument_root=? ORDER BY trading_day", (root,)):
        valid = bool(r["complete"]) and not r["degraded"]
        reason = None if valid else ("DEGRADED" if r["complete"] else (r["reason"] or "INCOMPLETE"))
        out.append({"day": r["trading_day"], "contract_id": r["contract_id"], "source": r["source"], "valid": valid,
                    "reason": reason, "fixture": bool(r["fixture"]),
                    "sha": _db_day_hash(con, r["source"], r["contract_id"], r["trading_day"]) if r["source"]
                    else contenthash.EMPTY})
    return out


def withheld_days(settings: Settings, con: sqlite3.Connection, root: str, after: date) -> list[dict]:
    """截止日之后、只有覆盖记录的交易日。有效 = 当日实际合约的原始线覆盖了全部预期的 RTH 5 分钟格，且未降级。"""
    inst = settings.instrument(root)
    by_day: dict[str, list] = {}
    for r in con.execute("SELECT trading_day, source, contract_id, timeframe, rth_slots_present, content_sha256"
                         " FROM withheld_coverage WHERE instrument_root=? AND trading_day>? ORDER BY trading_day",
                         (root, after.isoformat())):
        by_day.setdefault(r["trading_day"], []).append(r)
    if not by_day:
        return []
    out = []
    for d in sessions.trading_days(date.fromisoformat(min(by_day)), date.fromisoformat(max(by_day))):
        ds = d.isoformat()
        cid = contracts.active_contract(inst, d, settings.roll_rules)
        rows = [r for r in by_day.get(ds, []) if r["contract_id"] == cid]
        source = pick_source({r["source"] for r in rows}, settings.source_priority)
        rows = [r for r in rows if r["source"] == source]
        expected = len(sessions.expected_rth_slots(d))
        full = any(r["rth_slots_present"] >= expected for r in rows)
        sha = contenthash.EMPTY
        for r in rows:
            sha = contenthash.merge_hex(sha, r["content_sha256"])
        degraded = _degraded(con, source, ds)
        valid = full and not degraded and all(r["content_sha256"] for r in rows)
        reason = None if valid else ("NO_DATA" if not rows else "DEGRADED" if full and degraded
                                     else "NO_CONTENT_HASH" if full else "INCOMPLETE")
        out.append({"day": ds, "contract_id": cid, "source": source, "valid": valid, "reason": reason,
                    "fixture": False, "sha": sha})
    return out


def _segment(kind: str, seq: int, days: list[dict], status: str, role: str | None = None) -> dict:
    body = "\n".join(f"{d['day']}|{int(d['valid'])}|{d['source'] or ''}|{d['contract_id']}|{d['sha']}" for d in days)
    return {"kind": kind, "seq": seq, "start_day": days[0]["day"], "end_day": days[-1]["day"],
            "valid_days": sum(d["valid"] for d in days),
            "days": [[d["day"], int(d["valid"]), d["reason"]] for d in days],
            "content_sha256": contenthash.of_text(body), "status": status, "default_role": role}


def build(settings: Settings, con: sqlite3.Connection, root: str, cfg: ResearchConfig) -> dict:
    """算出切分（不写库）。放行的实时行情日（config/live_data.yaml）不进任何段：作者看得到它们，不能当封存。"""
    from .. import live_window
    lw = live_window.load(settings.project_root)
    vis = [d for d in visible_days(con, root) if d["fixture"] or not lw.is_live_day(root, d["day"])]
    if not vis:
        raise SplitError(f"{root} 没有任何交易日数据")
    fixture = all(d["fixture"] for d in vis)
    cut = settings.dev_visible_through
    if not fixture and cut is None:
        raise SplitError("真实数据需要先设置开发可见截止日（dev_visible_through）才能登记切分")
    if cut is not None and any(d["day"] > cut.isoformat() and not d["fixture"] for d in vis):
        raise SplitError("运行库里还有晚于开发可见截止日的真实数据：先运行 ingest 把它们移出")
    fvs = cfg.forward_validation_start.isoformat() if cfg.forward_validation_start else None
    dev = [d for d in vis if fvs is None or d["day"] < fvs]
    fwd = [d for d in vis if fvs is not None and d["day"] >= fvs]
    segments = []
    if dev:
        segments.append(_segment(DEVELOPMENT, 1, dev, "OPEN"))
    if fwd:
        segments.append(_segment(FORWARD, 1, fwd, "OPEN"))

    held = [d for d in (withheld_days(settings, con, root, cut) if cut is not None else [])
            if not lw.is_live_day(root, d["day"])]
    need, cur, seq = cfg.capsule_valid_days, [], 0
    usage = [("BLIND_SCREEN", cfg.blind_screen_capsules), ("CONFIRM", cfg.confirm_capsules)]
    roles = [r for r, n in usage for _ in range(n)]
    for d in held:
        cur.append(d)
        if sum(x["valid"] for x in cur) == need:
            seq += 1
            segments.append(_segment(CAPSULE, seq, cur, "SEALED", roles[seq - 1] if seq <= len(roles) else None))
            cur = []
    if cur:
        segments.append(_segment(REMAINDER, 1, cur, "SEALED"))

    definition = {
        "instrument_root": root, "dev_visible_through": cut.isoformat() if cut else None,
        "forward_validation_start": fvs, "capsule_valid_days": need,
        "default_usage": {"blind_screen_capsules": cfg.blind_screen_capsules, "confirm_capsules": cfg.confirm_capsules},
        "roll_rule_version": settings.instrument(root).roll_rule_version, "calendar_version": sessions.CALENDAR_VERSION,
        "valid_day_rule": "当日实际合约 RTH 完整且来源质量未降级",
        "first_day": vis[0]["day"], "last_visible_day": vis[-1]["day"],
        "last_withheld_day": held[-1]["day"] if held else None, "fixture": fixture}
    content = contenthash.of_text(*(f"{s['kind']}:{s['seq']}:{s['content_sha256']}" for s in segments))
    rid = "split_" + contenthash.of_text(json.dumps(definition, sort_keys=True), content)[:16]
    capsules = [s for s in segments if s["kind"] == CAPSULE]
    per_candidate = cfg.blind_screen_capsules + cfg.confirm_capsules
    return {"registry_id": rid, "instrument_root": root, "definition": definition, "content_sha256": content,
            "fixture": fixture, "segments": segments,
            "summary": {
                "development_days": sum(s["valid_days"] for s in segments if s["kind"] == DEVELOPMENT),
                "forward_validation_days": sum(s["valid_days"] for s in segments if s["kind"] == FORWARD),
                "capsules": len(capsules), "capsule_valid_days": sum(s["valid_days"] for s in capsules),
                "remainder_valid_days": sum(s["valid_days"] for s in segments if s["kind"] == REMAINDER),
                "candidates_supported": len(capsules) // per_candidate}}


def register(settings: Settings, con: sqlite3.Connection, root: str, cfg: ResearchConfig) -> dict:
    """登记切分。输入不变时得到同一个 registry_id，不新增记录。"""
    reg = build(settings, con, root, cfg)
    exists = con.execute("SELECT 1 FROM split_registries WHERE registry_id=?", (reg["registry_id"],)).fetchone()
    if exists is None:
        with con:
            con.execute("INSERT INTO split_registries VALUES (?,?,?,?,?,?)",
                        (reg["registry_id"], root, json.dumps(reg["definition"], ensure_ascii=False, sort_keys=True),
                         reg["content_sha256"], int(reg["fixture"]), int(time.time())))
            con.executemany(
                "INSERT INTO data_segments VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(reg["registry_id"], s["kind"], s["seq"], s["start_day"], s["end_day"], s["valid_days"],
                  json.dumps(s["days"]), s["content_sha256"], s["status"], s["default_role"]) for s in reg["segments"]])
    reg["created"] = exists is None
    return reg


def segment_days(reg: dict, kind: str, *, valid_only: bool = True) -> list[str]:
    return [d[0] for s in reg["segments"] if s["kind"] == kind for d in s["days"] if d[1] or not valid_only]
