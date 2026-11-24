-- 阶段 9：已批准的发布清单。清单和审计事件只追加；active 指针只由批准和回滚改写。
CREATE TABLE release_manifests (
    release_id        TEXT PRIMARY KEY,
    scope             TEXT NOT NULL,
    release_type      TEXT NOT NULL,            -- CANDIDATE | INITIAL_PAPER_BASELINE
    previous_release  TEXT,
    manifest_hash     TEXT NOT NULL,
    manifest_json     TEXT NOT NULL,
    approved_by       TEXT NOT NULL,
    approval_channel  TEXT NOT NULL,            -- CLI_TTY | CONTROL_CHANNEL | TEST
    approved_at_utc   INTEGER NOT NULL
);

CREATE TRIGGER release_manifests_no_update BEFORE UPDATE ON release_manifests
BEGIN SELECT RAISE(ABORT, 'release_manifests 只追加'); END;
CREATE TRIGGER release_manifests_no_delete BEFORE DELETE ON release_manifests
BEGIN SELECT RAISE(ABORT, 'release_manifests 只追加'); END;
CREATE TRIGGER release_events_no_update BEFORE UPDATE ON release_events
BEGIN SELECT RAISE(ABORT, 'release_events 只追加'); END;
CREATE TRIGGER release_events_no_delete BEFORE DELETE ON release_events
BEGIN SELECT RAISE(ABORT, 'release_events 只追加'); END;
