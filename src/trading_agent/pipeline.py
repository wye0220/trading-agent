"""导入流水线：导入文件 → 重建派生线 → 重算交易日状态 → （可选）生成可得性报告。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date
from pathlib import Path

from . import days, derive
from .config import Settings
from .ingest import HOLDOUT_NOT_SET, enforce_cutoff, ingest_files, new_run_id


def run_ingest(settings: Settings, con: sqlite3.Connection, *, run_id: str | None = None, kind: str = "ingest",
               through: date | None = None, write_report: bool = True, report_path: Path | None = None,
               rebuild: bool = False) -> dict:
    run_id = run_id or new_run_id("ing")
    cutoff = through or settings.dev_visible_through
    started = int(time.time())
    con.execute("INSERT OR IGNORE INTO ingest_runs (run_id, kind, started_at_utc) VALUES (?,?,?)",
                (run_id, kind, started))
    con.commit()
    summary, touched = ingest_files(settings, con, run_id, cutoff)
    moved = {"moved_after_cutoff": 0, "rows_deleted": 0}
    last_days: set[tuple[str, str]] = set()
    if cutoff:
        # 截止日是后来才设的：库里已有的、晚于它的真实 K 线移到库外（只留覆盖和内容摘要）
        moved = enforce_cutoff(settings, con, run_id, cutoff)
        if moved["rows_deleted"]:
            # 只留下还在库里的（截止日之前的，以及放行的实时行情）
            touched = {t for t in touched if t[2] <= cutoff.isoformat() or con.execute(
                "SELECT 1 FROM bars WHERE source=? AND contract_id=? AND trading_day=? LIMIT 1", t).fetchone()}
            last_days = {(r[0], r[1]) for r in con.execute(
                "SELECT instrument_root, MAX(trading_day) FROM trading_days WHERE fixture = 0"
                " GROUP BY instrument_root") if r[1]}
    if rebuild:
        # 换月规则等配置变化后：清空全部派生结果和交易日状态，从原始 K 线重算。原始 K 线不动。
        derive.clear_derived(con)
        for t in ("trading_days", "rth_days", "reference_levels"):
            con.execute(f"DELETE FROM {t}")
        con.execute("DELETE FROM data_anomalies WHERE category IN ('UNEXPECTED_GAP','THIN_BAR','WRONG_CONTRACT')")
        con.commit()
        touched = derive.all_raw_keys(con)
    affected = derive.rebuild(con, settings, touched)
    days.refresh(con, settings, affected | last_days)
    days.refresh_quality(con)
    summary = {"ok": True, "run_id": run_id, "data_root": str(settings.data_root),
               "dev_visible_through": cutoff.isoformat() if cutoff else None,
               "flags": [] if cutoff else [HOLDOUT_NOT_SET], **summary, **moved,
               "rebuilt": rebuild, "trading_days_refreshed": len(affected), "seconds": None}
    if cutoff:
        from . import live_window
        summary["live_window"] = live_window.effective(con, settings.project_root).describe()
    if write_report:
        from . import report
        path = report_path or settings.project_root / "reports" / "data_availability.md"
        report.write(settings, con, path, cutoff)
        summary["report"] = str(path)
    summary["seconds"] = round(time.time() - started, 1)
    con.execute("UPDATE ingest_runs SET finished_at_utc=?, summary_json=? WHERE run_id=?",
                (int(time.time()), json.dumps(summary, ensure_ascii=False), run_id))
    con.commit()
    return summary
