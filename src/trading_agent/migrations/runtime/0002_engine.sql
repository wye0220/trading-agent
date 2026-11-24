-- 引擎核心（阶段 3）。时刻为 UTC epoch 秒，价格为整数 tick。

CREATE TABLE engine_runs (
    run_id            TEXT PRIMARY KEY,
    scope             TEXT NOT NULL,
    strategy_family   TEXT NOT NULL,
    run_mode          TEXT NOT NULL,            -- fixture | historical_replay | pre_eval_shadow | live_shadow | paper
    risk_domain       TEXT NOT NULL,
    watermark_utc     INTEGER NOT NULL DEFAULT 0,   -- 最近一次决策时点；时钟只前进
    last_bar_end_utc  INTEGER NOT NULL DEFAULT 0,   -- 最近处理的信号 K 的收盘时刻
    market_watermark_utc INTEGER NOT NULL DEFAULT 0, -- 成交模型已消费到的子 K 结束时刻
    state_json        TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1,
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);

ALTER TABLE paper_orders ADD COLUMN signal_bar_end_utc INTEGER;
ALTER TABLE paper_orders ADD COLUMN armed_at_utc INTEGER;
ALTER TABLE paper_orders ADD COLUMN expires_at_utc INTEGER;
ALTER TABLE paper_orders ADD COLUMN trigger_ticks INTEGER;
ALTER TABLE paper_orders ADD COLUMN limit_ticks INTEGER;
ALTER TABLE paper_orders ADD COLUMN stop_ticks INTEGER;
ALTER TABLE paper_orders ADD COLUMN target_json TEXT;
ALTER TABLE paper_orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1;
ALTER TABLE paper_orders ADD COLUMN bars_seen INTEGER NOT NULL DEFAULT 0;
ALTER TABLE paper_orders ADD COLUMN flags_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE paper_orders ADD COLUMN end_reason TEXT;
CREATE INDEX paper_orders_run ON paper_orders (run_id, status);

ALTER TABLE paper_fills ADD COLUMN position_id TEXT;
ALTER TABLE paper_fills ADD COLUMN kind TEXT NOT NULL DEFAULT 'ENTRY';     -- ENTRY | EXIT
ALTER TABLE paper_fills ADD COLUMN reason TEXT;

CREATE TABLE paper_positions (
    position_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    scope             TEXT NOT NULL,
    risk_domain       TEXT NOT NULL,
    order_id          TEXT NOT NULL,
    setup_id          TEXT,
    contract_id       TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    side              TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    entry_ticks       INTEGER NOT NULL,
    entry_time_utc    INTEGER NOT NULL,
    initial_stop_ticks INTEGER NOT NULL,
    stop_ticks        INTEGER NOT NULL,
    target_ticks      INTEGER NOT NULL,
    flatten_at_utc    INTEGER NOT NULL,
    status            TEXT NOT NULL,            -- OPEN | CLOSED
    flags_json        TEXT NOT NULL DEFAULT '[]',
    version_tuple_json TEXT NOT NULL DEFAULT '{}',
    version           INTEGER NOT NULL DEFAULT 1,
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);
CREATE INDEX paper_positions_run ON paper_positions (run_id, status);

CREATE TABLE paper_trades (
    trade_id          TEXT PRIMARY KEY,         -- 等于 position_id：一个仓位一笔已完结交易
    run_id            TEXT NOT NULL,
    scope             TEXT NOT NULL,
    risk_domain       TEXT NOT NULL,
    setup_id          TEXT,
    contract_id       TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    side              TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    entry_ticks       INTEGER NOT NULL,
    entry_time_utc    INTEGER NOT NULL,
    exit_ticks        INTEGER NOT NULL,
    exit_time_utc     INTEGER NOT NULL,
    exit_reason       TEXT NOT NULL,            -- STOP | TARGET | FORCED_FLAT | REVERSAL
    initial_risk_ticks INTEGER NOT NULL,
    pnl_ticks         INTEGER NOT NULL,
    gross_usd         TEXT NOT NULL,
    fees_usd          TEXT NOT NULL,
    net_usd           TEXT NOT NULL,
    net_sign          INTEGER NOT NULL,
    r_initial         TEXT,                     -- 按该笔初始风险归一化
    r_research        TEXT,                     -- 按固定研究风险单位
    flags_json        TEXT NOT NULL DEFAULT '[]',
    version_tuple_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL
);
CREATE INDEX paper_trades_run_day ON paper_trades (run_id, trading_day, exit_time_utc);

CREATE TABLE explanation_requests (
    request_id        TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,            -- ScanExplanation | PretradeCheck | ...
    scope             TEXT,
    snapshot_id       TEXT,
    event_id          TEXT,
    context_json      TEXT NOT NULL,
    status            TEXT NOT NULL,            -- PENDING | DONE | DEAD
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    result_json       TEXT,
    version           INTEGER NOT NULL DEFAULT 1,
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);

-- 真实账户：按账户和交易日，跨策略共享
CREATE TABLE account_risk_days (
    account_id        TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    realized_loss_usd TEXT NOT NULL DEFAULT '0',    -- 当日已实现净亏损（正数表示亏损）
    reserved_usd      TEXT NOT NULL DEFAULT '0',    -- 已激活计划的风险预留
    stops             INTEGER NOT NULL DEFAULT 0,
    halted            INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1,
    updated_at_utc    INTEGER NOT NULL,
    PRIMARY KEY (account_id, trading_day)
);

CREATE TABLE account_risk_events (
    account_id        TEXT NOT NULL,
    source_id         TEXT NOT NULL,            -- 券商成交或订单 ID，用于去重
    trading_day       TEXT NOT NULL,
    exit_reason       TEXT NOT NULL,
    net_usd           TEXT NOT NULL,
    strategy_family   TEXT,
    created_at_utc    INTEGER NOT NULL,
    PRIMARY KEY (account_id, source_id)
);

CREATE TABLE risk_reservations (
    plan_id           TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL,
    trading_day       TEXT NOT NULL,
    strategy_family   TEXT,
    amount_usd        TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    status            TEXT NOT NULL,            -- RESERVED | RELEASED
    created_at_utc    INTEGER NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);
