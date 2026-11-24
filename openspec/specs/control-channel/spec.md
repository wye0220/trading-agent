# control-channel Specification

## Purpose

规定控制通道只执行适配器校验过来源、发送者已绑定的白名单命令，其余文字只当数据，每条消息写进只追加的审计；审批回显 manifest_hash，解冻要短期确认令牌，当日约束只能收紧风险，watch 轮询间隔可动态切换。

## Requirements

### Requirement: 命令白名单与身份校验
控制通道 SHALL 只接受 `plan`、`activate`、`watch-interval`、`note`、`context`、`freeze`、`unfreeze`、`approve`、`reject`、`confirm-memory` 和 `confirm`，且只接受适配器校验过来源、发送者是绑定用户的消息。其余文字 SHALL 只当数据。每条命令 SHALL 写审计。

#### Scenario: 未绑定的用户
- **WHEN** 未绑定的用户发来 `freeze`
- **THEN** 拒绝，原因 `UNAUTHORIZED`，状态不变，审计记录拒绝

### Requirement: 审批与解冻的二次确认
`approve` SHALL 要求回显 manifest_hash。`unfreeze` SHALL 要求 scope 和 freeze_event_id，先返回短期确认令牌；令牌 SHALL 绑定原请求和发送者，过期或重复使用 SHALL 不改变状态。

#### Scenario: 过期令牌
- **WHEN** 令牌过期后才 `confirm`
- **THEN** 拒绝，原因 `TOKEN_EXPIRED`，冻结保持

### Requirement: context 只能收紧风险
`context` SHALL 只能登记禁止新入场或降低手数上限的约束，不能放宽已批准的限额。禁止入场的约束 SHALL 冻结纸面入场并让人工计划预检给出 `BLOCKED_BY_RISK`，到期自动解除。

#### Scenario: FOMC 前不开新仓
- **WHEN** 发来 `context no-entry until 14:00`
- **THEN** 14:00 前纸面入场被挡，人工计划预检 `BLOCKED_BY_RISK`

### Requirement: 动态调整 watch 间隔
`watch-interval` SHALL 接受 60、300 或 `1m`、`5m`，watch 循环下一轮起使用新间隔。

#### Scenario: 切到 5 分钟
- **WHEN** 发来 `watch-interval 5m`
- **THEN** watch 下一轮按 300 秒间隔
