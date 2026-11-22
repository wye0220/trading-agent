"""SQLite 连接、编号 migration、带 version 校验的更新。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"
BUSY_TIMEOUT_MS = 10_000


class StaleVersion(RuntimeError):
    """更新携带的 version 与库中当前值不一致。"""


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
        con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    return con


def migrate(con: sqlite3.Connection, kind: str) -> list[str]:
    """应用尚未执行的编号 migration，返回本次新应用的编号。重复执行不改数据。"""
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at_utc INTEGER NOT NULL)")
    done = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
    applied = []
    for f in sorted((MIGRATIONS / kind).glob("[0-9]*.sql")):
        ver = f.stem
        if ver in done:
            continue
        con.executescript("BEGIN;\n" + f.read_text(encoding="utf-8") + "\nCOMMIT;")
        con.execute("INSERT INTO schema_migrations VALUES (?, ?)", (ver, int(time.time())))
        con.commit()
        applied.append(ver)
    return applied


def applied_migrations(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")]


def open_runtime(settings) -> sqlite3.Connection:
    con = connect(settings.runtime_db)
    migrate(con, "runtime")
    return con


def open_release(settings) -> sqlite3.Connection:
    con = connect(settings.release_db)
    migrate(con, "release")
    return con


def tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def versioned_update(con: sqlite3.Connection, table: str, key_col: str, key, expected_version: int,
                     **fields) -> int:
    """按 version 乐观锁更新一行，返回新 version。version 不符则抛 StaleVersion，记录不变。

    调用方负责把它和对应的事件写入放在同一事务里（with con:）。
    """
    if not table.isidentifier() or not key_col.isidentifier() or not all(k.isidentifier() for k in fields):
        raise ValueError("非法的表名或列名")
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = con.execute(
        f"UPDATE {table} SET {sets}{', ' if sets else ''}version = version + 1 "
        f"WHERE {key_col} = ? AND version = ?", (*fields.values(), key, expected_version))
    if cur.rowcount != 1:
        raise StaleVersion(f"{table}.{key_col}={key!r} 的 version 不是 {expected_version}")
    return expected_version + 1
