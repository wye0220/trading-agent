-- 运行库初始结构。时刻一律为 UTC epoch 秒（整数）；价格为整数 tick。
-- 阶段 1 读写行情相关的表；其余业务表先建结构，由注明的阶段填充。

-- ---------------------------------------------------------------- 导入
CREATE TABLE ingest_runs (
    run_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,              -- ingest | collect
    started_at_utc  INTEGER NOT NULL,
    finished_at_utc INTEGER,
    summary_json    TEXT
);

CREATE TABLE ingest_files (
    file_id        INTEGER PRIMARY KEY,
    path           TEXT NOT NULL UNIQUE,        -- 相对数据根目录
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    kind           TEXT NOT NULL,               -- dbn | csv | condition
    data_schema    TEXT,
    rows_processed INTEGER NOT NULL DEFAULT 0,  -- CSV：已处理到的数据行数
    prefix_sha256  TEXT,                        -- CSV：已处理部分的摘要，用于判断是否只是追加
    records        INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,               -- IMPORTED | UNSUPPORTED_SCHEMA | REJECTED
    detail         TEXT,
    run_id         TEXT NOT NULL,
    updated_at_utc INTEGER NOT NULL
);

-- ---------------------------------------------------------------- K 线（原始层与派生层）
CREATE TABLE bars (
    source                 TEXT NOT NULL,
    contract_id            TEXT NOT NULL,
    timeframe              TEXT NOT NULL,       -- 1m | 5m | 1h
    bar_start_utc          INTEGER NOT NULL,
    revision               INTEGER NOT NULL,
    instrument_root        TEXT NOT NULL,
    bar_end_utc            INTEGER NOT NULL,
    trading_day            TEXT NOT NULL,       -- YYYY-MM-DD
    session_type           TEXT NOT NULL,       -- RTH | ETH | MIX（1h 线可跨时段）
    open                   INTEGER NOT NULL,
    high                   INTEGER NOT NULL,
    low                    INTEGER NOT NULL,
    close                  INTEGER NOT NULL,
    volume                 INTEGER NOT NULL,
    available_at_utc       INTEGER NOT NULL,    -- 该 revision 的首次可见时刻；派生线即 known_at_utc
    available_at_estimated INTEGER NOT NULL,
    derived                INTEGER NOT NULL DEFAULT 0,
    constituent_count      INTEGER,             -- 派生线：参与聚合的下级 K 线数
    deps_json              TEXT,                -- 派生线：[[bar_start_utc, revision], ...]
    fixture                INTEGER NOT NULL DEFAULT 0,
    file_id                INTEGER,
    PRIMARY KEY (source, contract_id, timeframe, bar_start_utc, revision)
) WITHOUT ROWID;
CREATE INDEX bars_day ON bars (contract_id, timeframe, trading_day, source);

-- 来源附带的逐日质量状态（Databento condition.json）
CREATE TABLE day_quality (
    source      TEXT NOT NULL,
    day         TEXT NOT NULL,
    condition   TEXT NOT NULL,
    PRIMARY KEY (source, day)
);

-- 开发可见截止日之后的数据：只记覆盖，不存价格
CREATE TABLE withheld_coverage (
    source            TEXT NOT NULL,
    contract_id       TEXT NOT NULL,
    instrument_root   TEXT NOT NULL,
    timeframe         TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    bar_count         INTEGER NOT NULL,
    rth_slots_present INTEGER NOT NULL,         -- RTH 内有数据的 5 分钟格数
    PRIMARY KEY (source, contract_id, timeframe, trading_day)
);

-- 校验异常
CREATE TABLE data_anomalies (
    anomaly_id      INTEGER PRIMARY KEY,
    category        TEXT NOT NULL,
    source          TEXT,
    instrument_root TEXT,
    contract_id     TEXT,
    timeframe       TEXT,
    start_utc       INTEGER,
    end_utc         INTEGER,
    trading_day     TEXT,
    detail          TEXT NOT NULL DEFAULT '',
    file_id         INTEGER,
    run_id          TEXT,
    UNIQUE (category, source, contract_id, timeframe, start_utc, detail)
);
CREATE INDEX anomalies_day ON data_anomalies (instrument_root, trading_day, category);

-- 每个品种每个交易日：实际合约、所选来源、RTH 完整性
CREATE TABLE trading_days (
    instrument_root   TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    contract_id       TEXT NOT NULL,
    roll_rule_version TEXT NOT NULL,
    source            TEXT,                     -- 所选来源；没有任何来源时为 NULL
    rth_expected      INTEGER NOT NULL,
    rth_present       INTEGER NOT NULL,
    gap_count         INTEGER NOT NULL,
    thin_count        INTEGER NOT NULL,
    wrong_contract    INTEGER NOT NULL DEFAULT 0,
    degraded          INTEGER NOT NULL DEFAULT 0,
    early_close       INTEGER NOT NULL DEFAULT 0,
    complete          INTEGER NOT NULL,         -- 完整有效的 RTH 日
    reason            TEXT,
    fixture           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_root, trading_day)
);

-- 完整 RTH 日的高低差（供 ADR）
CREATE TABLE rth_days (
    source       TEXT NOT NULL,
    contract_id  TEXT NOT NULL,
    trading_day  TEXT NOT NULL,
    open         INTEGER NOT NULL,
    high         INTEGER NOT NULL,
    low          INTEGER NOT NULL,
    close        INTEGER NOT NULL,
    volume       INTEGER NOT NULL,
    complete     INTEGER NOT NULL,
    reason       TEXT,
    known_at_utc INTEGER NOT NULL,
    deps_json    TEXT NOT NULL,
    PRIMARY KEY (source, contract_id, trading_day)
);

CREATE TABLE reference_levels (
    instrument_root TEXT NOT NULL,
    trading_day     TEXT NOT NULL,
    contract_id     TEXT NOT NULL,
    source          TEXT,
    pdh             INTEGER,
    pdl             INTEGER,
    prev_rth_close  INTEGER,
    onh             INTEGER,
    onl             INTEGER,
    prev_day_status TEXT NOT NULL,              -- OK | UNAVAILABLE:<原因>
    overnight_status TEXT NOT NULL,
    as_of_utc       INTEGER NOT NULL,           -- 计算时点（当日 RTH 开盘）
    known_at_utc    INTEGER,
    deps_json       TEXT NOT NULL,
    PRIMARY KEY (instrument_root, trading_day)
);

-- ---------------------------------------------------------------- 快照与扫描
CREATE TABLE market_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    scope             TEXT NOT NULL,
    as_of_utc         INTEGER NOT NULL,
    trading_day       TEXT,
    contract_id       TEXT,
    source            TEXT,
    bar_count         INTEGER NOT NULL,
    latest_bar_end_utc      INTEGER,
    latest_bar_available_at_utc INTEGER,
    roll_rule_version TEXT NOT NULL,
    calendar_version  TEXT NOT NULL,
    content_sha256    TEXT NOT NULL,
    fixture           INTEGER NOT NULL DEFAULT 0,
    created_at_utc    INTEGER NOT NULL
);

CREATE TABLE scope_data_status (
    scope          TEXT NOT NULL,
    risk_domain    TEXT NOT NULL,
    blocked        INTEGER NOT NULL,            -- 1 = 不可新入场
    reason         TEXT,
    since_utc      INTEGER,
    version        INTEGER NOT NULL DEFAULT 1,
    updated_at_utc INTEGER NOT NULL,
    PRIMARY KEY (scope, risk_domain)
);

CREATE TABLE scan_events (
    event_id            TEXT PRIMARY KEY,
    scope               TEXT NOT NULL,
    strategy_family     TEXT NOT NULL,
    run_mode            TEXT NOT NULL,          -- fixture | historical_replay | pre_eval_shadow | live_shadow | paper | manual_actual
    risk_domain         TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    as_of_utc           INTEGER NOT NULL,
    snapshot_id         TEXT,
    contract_id         TEXT,
    signal_status       TEXT NOT NULL,          -- WAIT | SETUP_VALID | DATA_ERROR
    risk_status         TEXT NOT NULL DEFAULT 'NOT_EVALUATED',
    paper_action        TEXT NOT NULL DEFAULT 'NONE',
    version_tuple_json  TEXT NOT NULL DEFAULT '{}',
    strategy_state_json TEXT NOT NULL DEFAULT '{}',
    decision_json       TEXT NOT NULL DEFAULT '{}',
    created_at_utc      INTEGER NOT NULL
);
CREATE INDEX scan_events_scope ON scan_events (scope, as_of_utc);

-- ---------------------------------------------------------------- 纸面账本（阶段 3、4）
CREATE TABLE paper_orders (
    order_id        TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    risk_domain     TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    run_mode        TEXT NOT NULL,
    setup_id        TEXT,
    contract_id     TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    intent_json     TEXT NOT NULL,
    status          TEXT NOT NULL,
    version_tuple_json TEXT NOT NULL DEFAULT '{}',
    version         INTEGER NOT NULL DEFAULT 1,
    created_at_utc  INTEGER NOT NULL,
    updated_at_utc  INTEGER NOT NULL
);

CREATE TABLE paper_fills (
    fill_id         TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES paper_orders(order_id),
    scope           TEXT NOT NULL,
    risk_domain     TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    contract_id     TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    price_ticks     INTEGER NOT NULL,
    fill_time_utc   INTEGER NOT NULL,
    flags_json      TEXT NOT NULL DEFAULT '[]',
    fees_json       TEXT NOT NULL DEFAULT '{}',
    created_at_utc  INTEGER NOT NULL
);

-- ---------------------------------------------------------------- 人工轨（阶段 4）
CREATE TABLE trader_plans (
    plan_id         TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    strategy_family TEXT NOT NULL,
    contract_id     TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_ticks     INTEGER NOT NULL,
    stop_ticks      INTEGER,
    target_ticks    INTEGER,
    quantity        INTEGER NOT NULL,
    reason          TEXT,
    status          TEXT NOT NULL,              -- DRAFT | ACTIVE | PARTIALLY_FILLED | FILLED | CANCELLED | EXPIRED
    expires_at_utc  INTEGER,
    pretrade_json   TEXT NOT NULL DEFAULT '{}',
    version         INTEGER NOT NULL DEFAULT 1,
    submitted_at_utc INTEGER NOT NULL,
    updated_at_utc  INTEGER NOT NULL
);

CREATE TABLE manual_trades (
    trade_id        TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    contract_id     TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    price_ticks     INTEGER NOT NULL,
    executed_at_utc INTEGER NOT NULL,
    broker_order_id TEXT,
    broker_fill_id  TEXT,
    plan_id         TEXT,
    plan_link_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    source          TEXT NOT NULL,
    imported_at_utc INTEGER NOT NULL,
    UNIQUE (source, broker_fill_id)
);

CREATE TABLE manual_order_events (
    event_id        TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    broker_order_id TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    event_time_utc  INTEGER NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    source          TEXT NOT NULL,
    imported_at_utc INTEGER NOT NULL
);

CREATE TABLE broker_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    fetched_at_utc  INTEGER NOT NULL,
    source          TEXT NOT NULL,
    positions_json  TEXT NOT NULL DEFAULT '[]',
    open_orders_json TEXT NOT NULL DEFAULT '[]',
    fills_json      TEXT NOT NULL DEFAULT '[]',
    content_sha256  TEXT NOT NULL
);

CREATE TABLE discipline_alerts (
    alert_id        TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    rule            TEXT NOT NULL,
    plan_id         TEXT,
    broker_snapshot_id TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}',
    created_at_utc  INTEGER NOT NULL
);

CREATE TABLE trader_notes (
    note_id         TEXT PRIMARY KEY,
    event_id        TEXT,
    body            TEXT NOT NULL,
    author          TEXT NOT NULL,
    created_at_utc  INTEGER NOT NULL
);

-- ---------------------------------------------------------------- session、候选、记忆、评测（阶段 4、6A、6B、7、8）
CREATE TABLE sessions (
    session_id      TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    trading_day     TEXT NOT NULL,
    run_mode        TEXT NOT NULL,
    risk_domain     TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    status          TEXT NOT NULL,
    kpi_json        TEXT NOT NULL DEFAULT '{}',
    sealed_at_utc   INTEGER,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at_utc  INTEGER NOT NULL
);

CREATE TABLE candidates (
    candidate_id    TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    category        TEXT NOT NULL,
    eval_track      TEXT,
    status          TEXT NOT NULL,
    base_release    TEXT,
    body_json       TEXT NOT NULL DEFAULT '{}',
    manifest_hash   TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at_utc  INTEGER NOT NULL,
    updated_at_utc  INTEGER NOT NULL
);

CREATE TABLE candidate_ledger (
    entry_id        INTEGER PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    scope           TEXT NOT NULL,
    hypothesis_family TEXT,
    event           TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    alpha_spent     REAL,
    detail_json     TEXT NOT NULL DEFAULT '{}',
    created_at_utc  INTEGER NOT NULL
);

CREATE TABLE memory_items (
    item_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    scope           TEXT NOT NULL,
    statement_json  TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    confirmed_by    TEXT,
    status          TEXT NOT NULL,
    valid_until_utc INTEGER,
    created_at_utc  INTEGER NOT NULL,
    PRIMARY KEY (item_id, version)
);

CREATE TABLE evaluation_runs (
    evaluation_id   TEXT PRIMARY KEY,
    candidate_id    TEXT,
    scope           TEXT NOT NULL,
    protocol_id     TEXT,
    stage           TEXT,
    status          TEXT NOT NULL,
    result_json     TEXT NOT NULL DEFAULT '{}',
    version         INTEGER NOT NULL DEFAULT 1,
    created_at_utc  INTEGER NOT NULL
);

-- ---------------------------------------------------------------- 告警、冻结、死信（阶段 1 起使用 alerts）
CREATE TABLE alerts (
    alert_id        INTEGER PRIMARY KEY,
    level           TEXT NOT NULL,              -- CRITICAL | WARN | INFO
    kind            TEXT NOT NULL,
    scope           TEXT,
    message         TEXT NOT NULL,
    detail_json     TEXT NOT NULL DEFAULT '{}',
    run_id          TEXT,
    created_at_utc  INTEGER NOT NULL
);

CREATE TABLE freeze_events (
    freeze_event_id TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    risk_domain     TEXT NOT NULL,
    cause           TEXT NOT NULL,              -- DATA | MANUAL | DAILY_LOSS | MONITOR
    action          TEXT NOT NULL,              -- FREEZE | UNFREEZE
    reason          TEXT,
    actor           TEXT,
    created_at_utc  INTEGER NOT NULL
);

CREATE TABLE dead_letter_queue (
    task_id         TEXT PRIMARY KEY,
    task_type       TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    created_at_utc  INTEGER NOT NULL,
    updated_at_utc  INTEGER NOT NULL
);
