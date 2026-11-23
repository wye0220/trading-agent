-- 日复盘（阶段 6A）。

-- 按运行和时间取决策记录：封存和复盘都按交易日读
CREATE INDEX IF NOT EXISTS scan_events_run ON scan_events (run_id, as_of_utc);

CREATE TABLE session_reviews (
    session_id        TEXT PRIMARY KEY,
    scope             TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    account_id        TEXT,
    review_json       TEXT NOT NULL,            -- 通过 SessionReview schema 的对象
    review_sha256     TEXT NOT NULL,
    summary_json      TEXT NOT NULL DEFAULT '{}',
    rules_version     TEXT NOT NULL,
    superseded_json   TEXT NOT NULL DEFAULT '[]',   -- 被覆盖的旧版本的摘要
    version           INTEGER NOT NULL DEFAULT 1,
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);
CREATE INDEX session_reviews_day ON session_reviews (scope, trading_day);

CREATE TABLE observations (
    observation_id    TEXT PRIMARY KEY,         -- 由 session、机制、证据的摘要得出
    session_id        TEXT NOT NULL,
    scope             TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    track             TEXT NOT NULL,            -- STRATEGY | MANUAL | DATA_ENGINE
    mechanism         TEXT NOT NULL,
    evidence_json     TEXT NOT NULL DEFAULT '[]',
    detail_json       TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL
);
CREATE INDEX observations_mech ON observations (scope, mechanism, trading_day);

CREATE TABLE incident_drafts (
    incident_id       TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    scope             TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    kind              TEXT NOT NULL,            -- DATA_ERROR | ENGINE_ANOMALY | MAJOR_FILL_AMBIGUITY
    status            TEXT NOT NULL,            -- DRAFT（等用户确认后才进回归库）
    body_path         TEXT NOT NULL,
    body_sha256       TEXT NOT NULL,
    summary_json      TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);

CREATE TABLE memory_candidates (
    memory_candidate_id TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    note_id           TEXT,
    body_json         TEXT NOT NULL,            -- 通过 MemoryCandidate schema 的对象
    status            TEXT NOT NULL,            -- PENDING_CONFIRMATION
    created_at_utc    INTEGER NOT NULL
);
