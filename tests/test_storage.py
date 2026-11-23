import sqlite3
import threading

import pytest

import helpers as H
from trading_agent import db

RUNTIME_TABLES = {
    "market_snapshots", "reference_levels", "scan_events", "paper_orders", "paper_fills", "trader_plans",
    "manual_trades", "manual_order_events", "broker_snapshots", "discipline_alerts", "trader_notes", "sessions",
    "candidates", "candidate_ledger", "memory_items", "evaluation_runs", "alerts", "freeze_events",
    "dead_letter_queue", "bars", "ingest_files"}


def test_runtime_tables_exist(env):
    t = db.tables(env.con)
    assert RUNTIME_TABLES <= t
    cols = {r[1] for r in env.con.execute("PRAGMA table_info(scan_events)")}
    assert {"strategy_family", "strategy_state_json"} <= cols
    assert "revision" in {r[1] for r in env.con.execute("PRAGMA table_info(bars)")}
    for tbl in ("paper_orders", "trader_plans", "sessions", "candidates", "scope_data_status"):
        assert "version" in {r[1] for r in env.con.execute(f"PRAGMA table_info({tbl})")}
    assert db.applied_migrations(env.con)[0] == "0001_initial"


def test_release_db_is_separate(env):
    rel = db.open_release(env.settings)
    try:
        assert db.tables(rel) == {"schema_migrations", "active_pointers", "release_events"}
        assert "active_pointers" not in db.tables(env.con) and "release_events" not in db.tables(env.con)
        pk = [r[1] for r in rel.execute("PRAGMA table_info(active_pointers)") if r[5]]
        assert set(pk) == {"strategy_family", "instrument_root", "session_type", "timeframe"}
    finally:
        rel.close()
    assert env.settings.release_db != env.settings.runtime_db


def test_runtime_db_lives_in_data_root(env):
    assert env.settings.runtime_db.is_file()
    assert env.settings.data_root in env.settings.runtime_db.parents
    assert H.PROJECT.resolve() not in env.settings.runtime_db.resolve().parents
    assert not list(H.PROJECT.glob("*.sqlite")) and not list((H.PROJECT / "data").glob("*.sqlite*"))


def test_wal_and_busy_timeout(env):
    assert env.con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert env.con.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS


def test_reinit_keeps_data(env):
    env.con.execute("INSERT INTO alerts (level, kind, message, created_at_utc) VALUES ('INFO','T','x',1)")
    env.con.commit()
    assert db.migrate(env.con, "runtime") == []
    con2 = db.open_runtime(env.settings)
    assert con2.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    assert db.applied_migrations(con2) == ["0001_initial", "0002_engine", "0003_manual"]
    con2.close()


def test_reader_not_blocked_by_writer(env):
    """一个连接持有未提交的写事务时，另一个连接仍能读到已提交的数据。"""
    env.con.execute("INSERT INTO alerts (level, kind, message, created_at_utc) VALUES ('INFO','T','committed',1)")
    env.con.commit()
    env.con.execute("BEGIN IMMEDIATE")
    env.con.execute("INSERT INTO alerts (level, kind, message, created_at_utc) VALUES ('INFO','T','pending',2)")
    out = {}

    def read():
        c = db.connect(env.settings.runtime_db)
        try:
            out["n"] = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        except sqlite3.OperationalError as e:      # pragma: no cover
            out["err"] = e
        finally:
            c.close()

    t = threading.Thread(target=read)
    t.start()
    t.join(5)
    env.con.commit()
    assert out == {"n": 1}


def test_versioned_update_rejects_stale(env):
    env.con.execute("INSERT INTO sessions (session_id, scope, trading_day, run_mode, risk_domain, run_id, status,"
                    " created_at_utc) VALUES ('s1','pa_brooks:ES:RTH:5m','2025-11-25','fixture','fixture','r','OPEN',1)")
    env.con.commit()
    with env.con:
        assert db.versioned_update(env.con, "sessions", "session_id", "s1", 1, status="SEALED") == 2
    with pytest.raises(db.StaleVersion):
        with env.con:
            db.versioned_update(env.con, "sessions", "session_id", "s1", 1, status="REOPENED")
    assert tuple(env.con.execute("SELECT status, version FROM sessions").fetchone()) == ("SEALED", 2)
