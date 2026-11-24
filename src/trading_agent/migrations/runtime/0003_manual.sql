-- 人工轨（阶段 4）：计划审计、成交开平与关联、纪律事件、watch 状态。

ALTER TABLE trader_plans ADD COLUMN scope TEXT;
ALTER TABLE trader_plans ADD COLUMN setup_id TEXT;
ALTER TABLE trader_plans ADD COLUMN activated_at_utc INTEGER;
ALTER TABLE trader_plans ADD COLUMN filled_quantity INTEGER NOT NULL DEFAULT 0;
ALTER TABLE trader_plans ADD COLUMN version_tuple_json TEXT NOT NULL DEFAULT '{}';
CREATE INDEX trader_plans_account ON trader_plans (account_id, status);

CREATE TABLE plan_events (
    event_id       INTEGER PRIMARY KEY,
    plan_id        TEXT NOT NULL,
    from_status    TEXT,
    to_status      TEXT NOT NULL,
    at_utc         INTEGER NOT NULL,
    actor          TEXT,
    detail_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX plan_events_plan ON plan_events (plan_id, at_utc);

-- manual_trades 的一行是一笔成交的"开仓部分"或"平仓部分"（跨过零的成交拆成两行）
ALTER TABLE manual_trades ADD COLUMN fill_key TEXT;             -- 来源成交的去重键
ALTER TABLE manual_trades ADD COLUMN effect TEXT;               -- OPEN | CLOSE
ALTER TABLE manual_trades ADD COLUMN trading_day TEXT;
ALTER TABLE manual_trades ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0;
ALTER TABLE manual_trades ADD COLUMN match_rule TEXT;           -- BROKER_FILL_ID | SYNTHETIC_KEY
ALTER TABLE manual_trades ADD COLUMN suggested_plan_id TEXT;
ALTER TABLE manual_trades ADD COLUMN link_note TEXT;
ALTER TABLE manual_trades ADD COLUMN entry_ticks INTEGER;       -- 平仓部分：按先进先出对应的平均入场价（tick，可能含小数时取整前见 realized_usd）
ALTER TABLE manual_trades ADD COLUMN realized_usd TEXT;         -- 平仓部分：已实现净盈亏
ALTER TABLE manual_trades ADD COLUMN exit_kind TEXT;            -- 平仓部分：STOP | LIMIT | MARKET | UNKNOWN
ALTER TABLE manual_trades ADD COLUMN plan_adherence TEXT;       -- 平仓部分：COMPLIANT | VIOLATION | UNKNOWN
ALTER TABLE manual_trades ADD COLUMN strategy_alignment TEXT;
CREATE INDEX manual_trades_account ON manual_trades (account_id, contract_id, executed_at_utc);

ALTER TABLE manual_order_events ADD COLUMN contract_id TEXT;
ALTER TABLE manual_order_events ADD COLUMN side TEXT;
ALTER TABLE manual_order_events ADD COLUMN order_kind TEXT;     -- STOP | LIMIT | MARKET | STOP_LIMIT
ALTER TABLE manual_order_events ADD COLUMN price_ticks INTEGER;
ALTER TABLE manual_order_events ADD COLUMN quantity INTEGER;
CREATE INDEX manual_order_events_order ON manual_order_events (account_id, broker_order_id, event_time_utc);

CREATE TABLE discipline_events (
    event_id           TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL,
    trading_day        TEXT NOT NULL,
    type               TEXT NOT NULL,
    plan_id            TEXT,
    trade_id           TEXT,
    action_time_utc    INTEGER NOT NULL,
    plan_adherence     TEXT NOT NULL,           -- COMPLIANT | VIOLATION | UNKNOWN
    strategy_alignment TEXT NOT NULL,           -- ALIGNED | DEVIATES | UNKNOWN
    evidence_json      TEXT NOT NULL DEFAULT '{}',
    created_at_utc     INTEGER NOT NULL
);
CREATE INDEX discipline_events_day ON discipline_events (account_id, trading_day, type);

ALTER TABLE discipline_alerts ADD COLUMN trading_day TEXT;
ALTER TABLE discipline_alerts ADD COLUMN position_key TEXT;
ALTER TABLE discipline_alerts ADD COLUMN level TEXT;
ALTER TABLE discipline_alerts ADD COLUMN message TEXT;
ALTER TABLE discipline_alerts ADD COLUMN resolved_at_utc INTEGER;
CREATE INDEX discipline_alerts_key ON discipline_alerts (account_id, rule, position_key, created_at_utc);

CREATE TABLE watch_state (
    account_id     TEXT NOT NULL,
    key            TEXT NOT NULL,
    value_json     TEXT NOT NULL,
    updated_at_utc INTEGER NOT NULL,
    PRIMARY KEY (account_id, key)
);
