-- 发布库：只有 active 指针和审计事件。只有发布入口（阶段 9）可写。
CREATE TABLE active_pointers (
    strategy_family TEXT NOT NULL,
    instrument_root TEXT NOT NULL,
    session_type    TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    release_id      TEXT NOT NULL,
    manifest_hash   TEXT NOT NULL,
    entries_enabled INTEGER NOT NULL DEFAULT 1,
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at_utc  INTEGER NOT NULL,
    PRIMARY KEY (strategy_family, instrument_root, session_type, timeframe)
);

CREATE TABLE release_events (
    event_id        INTEGER PRIMARY KEY,
    scope           TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    release_id      TEXT,
    previous_release TEXT,
    manifest_hash   TEXT,
    actor           TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}',
    created_at_utc  INTEGER NOT NULL
);
