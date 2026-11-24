## ADDED Requirements

### Requirement: 告警分发、去重与静默
`ops --dispatch` SHALL 把未分发的告警按级别推送到通知适配器。同一去重键在静默窗口内 SHALL 只推送一次，其余记为 `SUPPRESSED`。每条告警的分发结果 SHALL 记录。纪律提醒 SHALL 在 watch 每轮之后即时推送。

#### Scenario: 重复告警
- **WHEN** 静默窗口内出现第二条同一去重键的告警
- **THEN** 它记为 `SUPPRESSED`，不推送

### Requirement: watch 停止告警
交易时段内 watch 心跳超过当前过期阈值没有更新时，健康检查 SHALL 记 CRITICAL `WATCH_STOPPED`。

#### Scenario: watch 停了
- **WHEN** 盘中 watch 心跳已超过 2 × 轮询间隔（至少 120 秒）
- **THEN** 记 CRITICAL `WATCH_STOPPED`
