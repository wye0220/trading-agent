-- Memory 的版本快照、头指针和事件（阶段 7）。memory_items 和 memory_candidates 沿用既有的表。

CREATE TABLE memory_versions (
    memory_version    TEXT PRIMARY KEY,         -- mem_<成员列表的摘要>：相同成员集得到同一个标识
    members_json      TEXT NOT NULL,            -- [[item_id, version], ...]，已排序
    item_count        INTEGER NOT NULL,
    created_at_utc    INTEGER NOT NULL
);

CREATE TABLE memory_head (
    head_key          TEXT PRIMARY KEY CHECK (head_key = 'global'),
    memory_version    TEXT NOT NULL REFERENCES memory_versions(memory_version),
    version           INTEGER NOT NULL DEFAULT 1,   -- 乐观锁
    updated_at_utc    INTEGER NOT NULL
);

CREATE TABLE memory_events (
    event_id          INTEGER PRIMARY KEY,
    event             TEXT NOT NULL,            -- PROPOSED | CONFIRMED | PENDING_EVALUATION | ACTIVATED | REJECTED | SUPERSEDED | EXPIRED | ROLLBACK
    memory_candidate_id TEXT,
    item_id           TEXT,
    item_version      INTEGER,
    from_memory_version TEXT,
    to_memory_version TEXT,
    actor             TEXT NOT NULL,
    detail_json       TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL
);
CREATE INDEX memory_events_item ON memory_events (item_id, event_id);
CREATE INDEX memory_events_cand ON memory_events (memory_candidate_id, event_id);
CREATE INDEX memory_items_status ON memory_items (status, scope);

CREATE TRIGGER memory_versions_no_update BEFORE UPDATE ON memory_versions
BEGIN SELECT RAISE(ABORT, 'memory_versions 不可修改'); END;
CREATE TRIGGER memory_versions_no_delete BEFORE DELETE ON memory_versions
BEGIN SELECT RAISE(ABORT, 'memory_versions 不可删除'); END;
CREATE TRIGGER memory_events_no_update BEFORE UPDATE ON memory_events
BEGIN SELECT RAISE(ABORT, 'memory_events 只追加'); END;
CREATE TRIGGER memory_events_no_delete BEFORE DELETE ON memory_events
BEGIN SELECT RAISE(ABORT, 'memory_events 只追加'); END;
