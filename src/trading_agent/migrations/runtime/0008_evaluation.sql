-- 独立评测（阶段 8）。evaluation_runs 沿用 0001 的表：每个登记的每个阶段一行，只放作者可见的摘要。

CREATE TABLE evaluation_protocols (
    protocol_id       TEXT PRIMARY KEY,         -- proto_<协议正文的摘要>
    body_json         TEXT NOT NULL,
    created_at_utc    INTEGER NOT NULL
);

CREATE TABLE evaluation_registrations (
    registration_id   TEXT PRIMARY KEY,         -- reg_<候选、基线、协议、窗口的摘要>
    batch_id          TEXT NOT NULL,            -- 同一次登记的候选共用一个批次（可以共用确认窗）
    candidate_id      TEXT NOT NULL,
    candidate_sha256  TEXT NOT NULL,
    scope             TEXT NOT NULL,
    instrument_root   TEXT NOT NULL,
    baseline_id       TEXT NOT NULL,
    registry_id       TEXT NOT NULL,
    protocol_id       TEXT NOT NULL REFERENCES evaluation_protocols(protocol_id),
    hypothesis_family TEXT NOT NULL,
    eval_track        TEXT NOT NULL,
    screen_json       TEXT NOT NULL,            -- 盲筛窗：胶囊序号、日期、内容摘要
    confirm_json      TEXT NOT NULL,            -- 确认窗
    created_at_utc    INTEGER NOT NULL
);
CREATE INDEX evaluation_registrations_cand ON evaluation_registrations (candidate_id, created_at_utc);

CREATE TABLE window_ledger (
    entry_id          INTEGER PRIMARY KEY,
    registry_id       TEXT NOT NULL,
    instrument_root   TEXT NOT NULL,
    capsule_seq       INTEGER NOT NULL,
    start_day         TEXT NOT NULL,            -- 行情的实际日期
    end_day           TEXT NOT NULL,
    purpose           TEXT NOT NULL,            -- SCREEN | CONFIRM
    event             TEXT NOT NULL,            -- ASSIGNED | UNSEALED | RETURNED
    registration_id   TEXT NOT NULL,
    candidate_id      TEXT NOT NULL,
    detail_json       TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL
);
CREATE INDEX window_ledger_days ON window_ledger (instrument_root, start_day, end_day);
CREATE INDEX window_ledger_reg ON window_ledger (registration_id, purpose, event);
CREATE INDEX evaluation_runs_cand ON evaluation_runs (candidate_id, stage);

CREATE TABLE incident_library (
    incident_id       TEXT PRIMARY KEY,
    scope             TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    kind              TEXT NOT NULL,
    status            TEXT NOT NULL,            -- CONFIRMED | INCOMPLETE
    body_path         TEXT NOT NULL,
    body_sha256       TEXT NOT NULL,
    expect_sha256     TEXT NOT NULL,            -- 期望结果（事故日的决策、订单、交易）的摘要
    confirmed_by      TEXT NOT NULL,
    note              TEXT,
    last_replay_json  TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);

CREATE TRIGGER evaluation_protocols_no_update BEFORE UPDATE ON evaluation_protocols
BEGIN SELECT RAISE(ABORT, 'evaluation_protocols 不可修改'); END;
CREATE TRIGGER evaluation_registrations_no_update BEFORE UPDATE ON evaluation_registrations
BEGIN SELECT RAISE(ABORT, 'evaluation_registrations 不可修改'); END;
CREATE TRIGGER evaluation_registrations_no_delete BEFORE DELETE ON evaluation_registrations
BEGIN SELECT RAISE(ABORT, 'evaluation_registrations 不可删除'); END;
CREATE TRIGGER window_ledger_no_update BEFORE UPDATE ON window_ledger
BEGIN SELECT RAISE(ABORT, 'window_ledger 只追加'); END;
CREATE TRIGGER window_ledger_no_delete BEFORE DELETE ON window_ledger
BEGIN SELECT RAISE(ABORT, 'window_ledger 只追加'); END;
