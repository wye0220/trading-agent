-- 聚合演化与候选台账（阶段 6B）。candidates 和 candidate_ledger 两张表在 0001 里已有。

CREATE TABLE evolve_runs (
    evolve_id         TEXT PRIMARY KEY,         -- 由 scope 和输入摘要得出：同样的输入得到同一个标识
    scope             TEXT NOT NULL,
    as_of_day         TEXT NOT NULL,            -- 窗口里最晚的交易日
    input_sha256      TEXT NOT NULL,
    result            TEXT NOT NULL,            -- CANDIDATES | NO_CANDIDATE
    summary_json      TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);
CREATE INDEX evolve_runs_scope ON evolve_runs (scope, updated_at_utc);

CREATE TABLE observation_clusters (
    evolve_id         TEXT NOT NULL REFERENCES evolve_runs(evolve_id),
    cluster_id        TEXT NOT NULL,            -- 由 scope、机制、原因、差异键得出
    scope             TEXT NOT NULL,
    strategy_family   TEXT NOT NULL,
    mechanism         TEXT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    diff_key          TEXT,                     -- 没有白名单内的键时为空
    days              INTEGER NOT NULL,         -- 出现的不同交易日数
    occurrences       INTEGER NOT NULL,
    counter_days      INTEGER,                  -- 反例天数；空 = 这个机制算不出反例
    status            TEXT NOT NULL,            -- OBSERVATION | PROMOTABLE
    group_id          TEXT,                     -- 同家族跨品种的候选组
    stats_json        TEXT NOT NULL DEFAULT '{}',
    examples_json     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (evolve_id, cluster_id)
);
CREATE INDEX observation_clusters_key ON observation_clusters (strategy_family, mechanism, reason, diff_key);

CREATE TABLE candidate_queue (
    candidate_id      TEXT PRIMARY KEY,
    scope             TEXT NOT NULL,
    eval_track        TEXT NOT NULL,            -- PRUNING_DEFENSIVE | ALPHA_EXPANSION
    status            TEXT NOT NULL,            -- QUEUED | SUPERSEDED | TAKEN
    base_release      TEXT NOT NULL,
    inputs_json       TEXT NOT NULL DEFAULT '{}',   -- 排序公式的输入：证据天数、反例占比、时段集中度
    effect_json       TEXT,                     -- 开发段加向前验证段的效应估计；空 = 还没估计
    score             REAL,
    enqueued_at_utc   INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);
CREATE INDEX candidate_queue_scope ON candidate_queue (scope, status);

-- 前向预演看过的日期：这些日期不得再进入该候选的正式确认窗
CREATE TABLE pre_eval_shadow_days (
    candidate_id      TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    result_json       TEXT NOT NULL DEFAULT '{}',   -- 只有执行异常和可运行性，没有盈亏
    created_at_utc    INTEGER NOT NULL,
    PRIMARY KEY (candidate_id, trading_day)
);
