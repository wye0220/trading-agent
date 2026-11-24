# release-monitor Specification

## Purpose

规定发布后每个交易日收盘对每个有 active 指针的 scope 计算激活以来的滚动指标，与清单里冻结的回滚触发条件比较，触发即冻结该 scope 的纸面入场并告警，回滚与否由用户决定。

## Requirements

### Requirement: 发布后滚动指标
`monitor` SHALL 在每个交易日收盘后为每个有 active 指针的 scope 计算该版本激活以来的滚动指标：净 R／日、回撤、成交频率、信号频率、`AMBIGUOUS_BAR` 占比，并与清单里冻结的预期区间比较。首次纸面基线 SHALL 与阶段 5 开发段基线报告的预期区间比较。

#### Scenario: 数据不够
- **WHEN** 激活以来的有效交易日少于滚动窗口
- **THEN** 只报告指标，频率类触发条件不判定

### Requirement: 触发即冻结并告警
任一 `rollback_trigger` 条件触发时，`monitor` SHALL 自动冻结该 scope 的纸面入场（原因 `MONITOR`）并发告警；回滚或解冻由你决定。同一 scope、同一交易日的监控 SHALL 幂等。

#### Scenario: 回撤超限
- **WHEN** 激活以来的回撤超过清单里的限额
- **THEN** scope 冻结，原因 `MONITOR`，告警写明触发条件和数值
