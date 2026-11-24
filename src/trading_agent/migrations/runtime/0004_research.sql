-- 回放与基线（阶段 5）。

-- 留在库外的 K 线的内容摘要：逐根 sha256 当作 256 位整数相加取模，与顺序无关。
ALTER TABLE withheld_coverage ADD COLUMN content_sha256 TEXT;

CREATE TABLE split_registries (
    registry_id       TEXT PRIMARY KEY,         -- 由登记内容的摘要得出
    instrument_root   TEXT NOT NULL,
    definition_json   TEXT NOT NULL,            -- 边界、胶囊长度、截止日、数据范围
    content_sha256    TEXT NOT NULL,            -- 全部段的日期清单与内容摘要的摘要
    fixture           INTEGER NOT NULL DEFAULT 0,
    created_at_utc    INTEGER NOT NULL
);

CREATE TABLE data_segments (
    registry_id       TEXT NOT NULL REFERENCES split_registries(registry_id),
    kind              TEXT NOT NULL,            -- DEVELOPMENT | FORWARD_VALIDATION | CAPSULE | REMAINDER
    seq               INTEGER NOT NULL,
    start_day         TEXT NOT NULL,
    end_day           TEXT NOT NULL,
    valid_days        INTEGER NOT NULL,
    days_json         TEXT NOT NULL,            -- [[交易日, 是否有效, 无效原因], ...]
    content_sha256    TEXT NOT NULL,
    status            TEXT NOT NULL,            -- OPEN | SEALED
    default_role      TEXT,                     -- 默认取用顺序里的用途：BLIND_SCREEN | CONFIRM
    PRIMARY KEY (registry_id, kind, seq)
);

CREATE TABLE baselines (
    baseline_id       TEXT PRIMARY KEY,         -- 由冻结清单的摘要得出
    scope             TEXT NOT NULL,
    strategy_family   TEXT NOT NULL,
    status            TEXT NOT NULL,            -- PENDING | FROZEN | NON_DETERMINISTIC
    manifest_json     TEXT NOT NULL,
    manifest_sha256   TEXT NOT NULL,
    registry_id       TEXT,
    run_id            TEXT,
    digests_json      TEXT NOT NULL DEFAULT '{}',
    flags_json        TEXT NOT NULL DEFAULT '[]',
    version           INTEGER NOT NULL DEFAULT 1,
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);
CREATE INDEX baselines_scope ON baselines (scope, created_at_utc);

CREATE TABLE research_runs (
    run_id            TEXT PRIMARY KEY,
    baseline_id       TEXT NOT NULL REFERENCES baselines(baseline_id),
    scope             TEXT NOT NULL,
    role              TEXT NOT NULL,            -- BASE | VERIFY | VARIANT
    variant_id        TEXT,
    track             TEXT,                     -- PRUNING_DEFENSIVE | ALPHA_EXPANSION
    diff_json         TEXT NOT NULL DEFAULT '{}',
    start_day         TEXT NOT NULL,
    end_day           TEXT NOT NULL,
    status            TEXT NOT NULL,            -- RUNNING | DONE
    digests_json      TEXT NOT NULL DEFAULT '{}',
    summary_json      TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);
CREATE INDEX research_runs_baseline ON research_runs (baseline_id, role);
