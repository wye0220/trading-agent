## ADDED Requirements

### Requirement: 封存条件
系统 SHALL 只在一个运行的一个交易日满足以下条件时封存它的 session：该日的截止强平时刻已过（按运行的水位判断），且该运行在该日没有未平的仓位和工作中的挂单。封存 MUST 写入当日指标并记录封存时刻。

#### Scenario: 当日还在进行
- **WHEN** 运行的水位早于该日的截止强平时刻
- **THEN** 封存被拒绝，原因是 `SESSION_IN_PROGRESS`

#### Scenario: 仓位无法平掉
- **WHEN** 截止之后仓位仍未平（无法定价）
- **THEN** 封存被拒绝，原因是 `POSITION_STILL_OPEN`

#### Scenario: 没有数据的交易日
- **WHEN** 该运行在该日没有任何决策记录
- **THEN** 封存被拒绝，原因是 `NO_ACTIVITY`

### Requirement: 封存幂等且不可改
已封存的 session 再次封存 SHALL 是空操作，返回原有记录。封存后当日指标 MUST NOT 被改写。

#### Scenario: 重复封存
- **WHEN** 对同一个运行、同一交易日封存两次
- **THEN** 只有一条 session，封存时刻和指标与第一次相同

### Requirement: 保留数据不进普通 session
真实数据的交易日晚于开发可见截止日（或截止日没有设置）时，系统 MUST NOT 为它封存普通 session。

#### Scenario: 截止日之后
- **WHEN** 请求封存一个晚于截止日的真实数据交易日
- **THEN** 封存被拒绝，原因是 `HOLDOUT_PROTECTED`
