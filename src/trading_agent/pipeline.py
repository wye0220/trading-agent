"""导入流水线：导入文件 → 重建派生线 → 重算交易日状态 → （可选）生成可得性报告。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date
from pathlib import Path

from . import days, derive
from .config import Settings
from .ingest import HOLDOUT_NOT_SET, ingest_files, new_run_id


def run_ingest(settings: Settings, con: sqlite3.Connection, *, run_id: str | None = None, kind: str = "ingest",
               through: date | None = None, write_report: bool = True, report_path: Path | None = None) -> dict:
    run_id = run_id or new_run_id("ing")
    cutoff = through or settings.dev_visible_through
    started = int(time.time())
    con.execute("INSERT OR IGNORE INTO ingest_runs (run_id, kind, started_at_utc) VALUES (?,?,?)",
                (run_id, kind, started))
    con.commit()
    summary, touched = ingest_files(settings, con, run_id, cutoff)
    affected = derive.rebuild(con, settings, touched)
    days.refresh(con, settings, affected)
    days.refresh_quality(con)
    summary = {"ok": True, "run_id": run_id, "data_root": str(settings.data_root),
               "dev_visible_through": cutoff.isoformat() if cutoff else None,
               "flags": [] if cutoff else [HOLDOUT_NOT_SET], **summary,
               "trading_days_refreshed": len(affected), "seconds": None}
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
