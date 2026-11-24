-- 阶段 9：发布版本（发布库的镜像和工作台）、版本事件、影子登记、发布后监控。
-- 发布库才是 active 指针的唯一来源；这里的状态在批准成功后更新，release --status 以发布库为准修正。

CREATE TABLE release_versions (
    release_id        TEXT PRIMARY KEY,         -- rel_<scope、类型、候选或基线、上一版本、配置差异的摘要>
    scope             TEXT NOT NULL,
    release_type      TEXT NOT NULL,            -- CANDIDATE | INITIAL_PAPER_BASELINE
    candidate_id      TEXT,
    baseline_id       TEXT,
    previous_release  TEXT,
    status            TEXT NOT NULL,            -- PROPOSED | APPROVED | ACTIVE | ROLLED_BACK | SUPERSEDED | ABANDONED
    manifest_json     TEXT NOT NULL,
    manifest_hash     TEXT NOT NULL,
    activated_at_utc  INTEGER,
    version           INTEGER NOT NULL DEFAULT 1,
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);
CREATE INDEX release_versions_scope ON release_versions (scope, status);

CREATE TABLE release_version_events (
    entry_id          INTEGER PRIMARY KEY,
    release_id        TEXT,
    candidate_id      TEXT,
    scope             TEXT NOT NULL,
    event             TEXT NOT NULL,
    from_status       TEXT,
    to_status         TEXT,
    actor             TEXT,
    detail_json       TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL
);
CREATE INDEX release_version_events_rel ON release_version_events (release_id, entry_id);

CREATE TABLE shadow_registrations (
    shadow_id         TEXT PRIMARY KEY,         -- shd_<候选、基准版本、计划的摘要>
    candidate_id      TEXT NOT NULL,
    scope             TEXT NOT NULL,
    base_release      TEXT NOT NULL,            -- 基线臂所用的 active 版本（或 baseline:<id>）
    plan_json         TEXT NOT NULL,            -- 登记时冻结：起始日、有效日数、最低成交、检查项、窗口性质、非劣效
    plan_sha256       TEXT NOT NULL,
    base_run_id       TEXT NOT NULL,
    cand_run_id       TEXT NOT NULL,
    created_at_utc    INTEGER NOT NULL
);
CREATE INDEX shadow_registrations_cand ON shadow_registrations (candidate_id, created_at_utc);

CREATE TABLE shadow_results (
    shadow_id         TEXT PRIMARY KEY REFERENCES shadow_registrations(shadow_id),
    status            TEXT NOT NULL,            -- PASSED | REJECTED | EVIDENCE_INSUFFICIENT
    result_json       TEXT NOT NULL,
    result_sha256     TEXT NOT NULL,
    created_at_utc    INTEGER NOT NULL
);

CREATE TABLE monitor_runs (
    monitor_id        TEXT PRIMARY KEY,         -- mon_<scope、版本、交易日的摘要>：同一天重跑是空操作
    scope             TEXT NOT NULL,
    release_id        TEXT NOT NULL,
    as_of_day         TEXT NOT NULL,
    metrics_json      TEXT NOT NULL,
    breaches_json     TEXT NOT NULL DEFAULT '[]',
    action            TEXT NOT NULL,            -- NONE | FROZEN | ALREADY_FROZEN
    created_at_utc    INTEGER NOT NULL
);

CREATE TRIGGER release_version_events_no_update BEFORE UPDATE ON release_version_events
BEGIN SELECT RAISE(ABORT, 'release_version_events 只追加'); END;
CREATE TRIGGER release_version_events_no_delete BEFORE DELETE ON release_version_events
BEGIN SELECT RAISE(ABORT, 'release_version_events 只追加'); END;
CREATE TRIGGER shadow_registrations_no_update BEFORE UPDATE ON shadow_registrations
BEGIN SELECT RAISE(ABORT, 'shadow_registrations 不可修改'); END;
CREATE TRIGGER shadow_registrations_no_delete BEFORE DELETE ON shadow_registrations
BEGIN SELECT RAISE(ABORT, 'shadow_registrations 不可删除'); END;
CREATE TRIGGER shadow_results_no_update BEFORE UPDATE ON shadow_results
BEGIN SELECT RAISE(ABORT, 'shadow_results 不可修改'); END;
CREATE TRIGGER shadow_results_no_delete BEFORE DELETE ON shadow_results
BEGIN SELECT RAISE(ABORT, 'shadow_results 不可删除'); END;
CREATE TRIGGER monitor_runs_no_update BEFORE UPDATE ON monitor_runs
BEGIN SELECT RAISE(ABORT, 'monitor_runs 只追加'); END;
