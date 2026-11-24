# ops-scheduling Specification

## Purpose

规定盘中节拍以运行锁和数据水位去重、只在新的 5 分钟线收线且可用后扫描并推进影子，以及盘后的封存复盘、账本检查、发布后监控和每周最后一个交易日的聚合。

## Requirements

### Requirement: 盘中节拍按收线去重
`ops --tick` SHALL 持有运行锁，对 `config/ops.yaml` 登记的每个 scope 只在出现新的、已可用的 5 分钟线时扫描一次，并推进影子；同一根线 SHALL 不重复处理。成功的水位和心跳 SHALL 持久化。

#### Scenario: 没有新线
- **WHEN** 上次处理之后没有新的已收线 5 分钟线
- **THEN** 这个 scope 跳过，状态 `NO_NEW_BAR`

#### Scenario: 并发运行
- **WHEN** 另一个节拍还持有运行锁
- **THEN** 这次运行直接退出，状态 `LOCKED`

#### Scenario: 睡眠后补跑
- **WHEN** 停机之后一次处理多根落下的线
- **THEN** 落下的信号只进历史账本，过期的挂单不会被恢复

### Requirement: 行情断流告警
RTH 盘中最新可用 5 分钟线落后超过 2 根时 SHALL 记 CRITICAL 告警；落后 1–2 根时 SHALL 记 WARN。

#### Scenario: 断流
- **WHEN** 盘中最新线比此刻落后 3 根
- **THEN** 记 CRITICAL `DATA_STALE`

### Requirement: 盘后流水
`ops --daily` SHALL 在交易日收盘后：确认当日 RTH 线到齐、封存并复盘各 scope 的常驻纸面运行和人工轨、检查账本一致、运行发布后监控。前向保留窗口内的复盘 SHALL 跳过，只记运行健康。

#### Scenario: 线没到齐
- **WHEN** 当日 RTH 线不完整
- **THEN** 不封存，记 WARN，状态 `INCOMPLETE_DAY`

### Requirement: 周度聚合
`ops --weekly` SHALL 只在每周最后一个交易日按 scope 运行聚合；其他日子 SHALL 跳过。

#### Scenario: 不是周末交易日
- **WHEN** 今天不是本周最后一个交易日
- **THEN** 状态 `NOT_LAST_TRADING_DAY`
