-- 阶段 10：运维状态（心跳、水位、覆盖值）、运行锁、告警分发、控制命令审计、确认令牌、context 约束。

CREATE TABLE ops_state (
    key               TEXT PRIMARY KEY,         -- heartbeat:tick、watermark:<scope>、watch_interval_override 等
    value_json        TEXT NOT NULL,
    updated_at_utc    INTEGER NOT NULL
);

CREATE TABLE ops_locks (
    name              TEXT PRIMARY KEY,
    holder            TEXT NOT NULL,
    acquired_at_utc   INTEGER NOT NULL,
    expires_at_utc    INTEGER NOT NULL
);

CREATE TABLE alert_deliveries (
    alert_id          INTEGER PRIMARY KEY REFERENCES alerts(alert_id),
    dedup_key         TEXT NOT NULL,
    status            TEXT NOT NULL,            -- SENT | SUPPRESSED | FAILED | NOT_PUSHED
    channel           TEXT NOT NULL,
    detail            TEXT,
    created_at_utc    INTEGER NOT NULL
);
CREATE INDEX alert_deliveries_key ON alert_deliveries (dedup_key, created_at_utc);

CREATE TABLE control_commands (
    command_id        TEXT PRIMARY KEY,
    channel           TEXT NOT NULL,            -- CLI | TELEGRAM | TEST
    user_id           TEXT,
    message_ref       TEXT,                     -- 适配器给的消息编号（去重）
    command           TEXT,
    args_json         TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL,            -- DONE | REFUSED | PENDING_CONFIRM | ERROR
    reason            TEXT,
    result_json       TEXT NOT NULL DEFAULT '{}',
    created_at_utc    INTEGER NOT NULL
);
CREATE UNIQUE INDEX control_commands_msg ON control_commands (channel, message_ref) WHERE message_ref IS NOT NULL;

CREATE TABLE confirm_tokens (
    token             TEXT PRIMARY KEY,
    command_id        TEXT NOT NULL REFERENCES control_commands(command_id),
    channel           TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    action_json       TEXT NOT NULL,
    expires_at_utc    INTEGER NOT NULL,
    used_at_utc       INTEGER
);

CREATE TABLE context_constraints (
    constraint_id     TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,            -- NO_ENTRY | MAX_QTY
    scope             TEXT NOT NULL,            -- 四维 scope，或 ALL
    value_json        TEXT NOT NULL DEFAULT '{}',
    active_from_utc   INTEGER NOT NULL,
    until_utc         INTEGER NOT NULL,
    created_by        TEXT NOT NULL,
    command_id        TEXT,
    created_at_utc    INTEGER NOT NULL
);

CREATE TRIGGER control_commands_no_update BEFORE UPDATE ON control_commands
BEGIN SELECT RAISE(ABORT, 'control_commands 只追加'); END;
CREATE TRIGGER control_commands_no_delete BEFORE DELETE ON control_commands
BEGIN SELECT RAISE(ABORT, 'control_commands 只追加'); END;
