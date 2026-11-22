"""Webull 只读采集：取实际合约已收线的 K 线，追加到按交易日分的 CSV，然后增量入库。

一次运行做一件事；调度（每 5 分钟、收线后约 10 秒）在外部。失败时不写任何行。
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import contracts, sessions
from .bars import TIMEFRAME_SECONDS
from .config import Scope, Settings
from .sources import csv_source
from .sources.webull import FetchError

SOURCE = "webull"


def csv_path(settings: Settings, root: str, contract_id: str, day: str) -> Path:
    return settings.data_root / SOURCE / root / contract_id / f"{day}.csv"


def _norm(v) -> str:
    """价格、成交量的规范文本，用于比较内容是否变化。"""
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return str(v).strip()
    return format(d.normalize(), "f")


def _existing(path: Path) -> dict[tuple[str, str], tuple]:
    """已保存的 CSV：(timeframe, bar_start_utc) -> 最后一行的内容。"""
    out = {}
    if path.is_file():
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                out[(r["timeframe"], r["bar_start_utc"])] = tuple(_norm(r[k]) for k in ("open", "high", "low", "close", "volume"))
    return out


def _append(path: Path, rows: list[list[str]]):
    """只追加，不改写已有内容。"""
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if new:
        w.writerow(csv_source.COLUMNS)
    w.writerows(rows)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
        f.flush()
        os.fsync(f.fileno())


def _fail(con, scope, run_id, code, message, now) -> dict:
    with con:
        con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, run_id, created_at_utc)"
                    " VALUES ('WARN','COLLECT_FAILED',?,?,?,?,?)",
                    (str(scope), f"collect 失败：{code} {message}".strip(), json.dumps({"code": code}), run_id, now))
    return {"ok": False, "run_id": run_id, "scope": str(scope), "error": code, "message": message,
            "rows_written": 0, "files": []}


def run_collect(settings: Settings, con: sqlite3.Connection, scope: Scope, adapter, *, now: int | None = None,
                run_id: str | None = None, ingest: bool = True) -> dict:
    from .ingest import new_run_id
    from .pipeline import run_ingest

    now = int(time.time()) if now is None else now
    run_id = run_id or new_run_id("col")
    cfg = settings.collect
    tf = cfg["timeframe"]
    size = TIMEFRAME_SECONDS[tf]
    root = scope.instrument_root
    inst = settings.instrument(root)
    day = sessions.trading_day_at(now)
    cid = contracts.active_contract(inst, day, settings.roll_rules)

    back = day
    for _ in range(cfg["lookback_trading_days"] - 1):
        back = sessions.prev_trading_day(back)
    start = sessions.eth_bounds(back)[0]
    count = min(cfg["max_bars_per_request"], (now - start) // size + 2)

    try:
        returned, rows = adapter.fetch(cid, tf, count, start, now)
    except FetchError as e:
        return _fail(con, scope, run_id, e.code, str(e), now)
    try:
        got = contracts.normalize(returned, datetime.fromtimestamp(now, timezone.utc).date()) if returned else None
    except contracts.ContractError:
        got = None
    if got != cid:
        return _fail(con, scope, run_id, "UNEXPECTED_CONTRACT", f"请求 {cid}，返回 {returned!r}", now)
    if not rows:
        return _fail(con, scope, run_id, "EMPTY_RESPONSE", f"{cid} 没有返回任何 K 线", now)

    fetched_iso = sessions.iso(now)
    per_file: dict[Path, list[list[str]]] = {}
    cache: dict[Path, dict] = {}
    stats = {"rows_fetched": len(rows), "skipped_unclosed": 0, "skipped_outside": 0, "skipped_existing": 0,
             "new_bars": 0, "revisions": 0}
    for r in rows:
        s = int(r["start"])
        if s + size > now:
            stats["skipped_unclosed"] += 1          # 取数时刻尚未收线：不保存
            continue
        d, sess = sessions.classify(s)
        if d is None or (sess == "ETH" and not cfg["include_eth"]):
            stats["skipped_outside"] += 1
            continue
        path = csv_path(settings, root, cid, d.isoformat())
        if path not in cache:
            cache[path] = _existing(path)
        key = (tf, sessions.iso(s))
        content = tuple(_norm(r[k]) for k in ("open", "high", "low", "close", "volume"))
        prev = cache[path].get(key)
        if prev == content:
            stats["skipped_existing"] += 1
            continue
        stats["revisions" if prev is not None else "new_bars"] += 1
        cache[path][key] = content
        per_file.setdefault(path, []).append(
            [root, cid, tf, sessions.iso(s), *content, SOURCE, sessions.iso(s + size), fetched_iso])

    for path, new_rows in sorted(per_file.items()):
        _append(path, new_rows)
    written = sum(len(v) for v in per_file.values())
    out = {"ok": True, "run_id": run_id, "scope": str(scope), "contract_id": cid, "timeframe": tf,
           "fetched_at_utc": fetched_iso, **stats, "rows_written": written,
           "files": [str(p.relative_to(settings.data_root)) for p in sorted(per_file)]}
    if ingest and written:
        out["ingest"] = run_ingest(settings, con, run_id=run_id, kind="collect", write_report=False)
    return out
