## ADDED Requirements

### Requirement: 冻结按原因记录和解除
`freeze` SHALL 阻止指定 scope 的新入场，可选撤掉未成交的入场单。冻结原因 SHALL 分开记录：`DATA` 在数据恢复后自动解除；`DAILY_LOSS` 在下一个交易日自动解除；`MANUAL`、`MONITOR` 只能由你在交互终端里解除；`RELEASE` 只能由新的批准解除。纸面订单提交前 SHALL 重读最新冻结状态。

#### Scenario: 冻结后的入场冲突
- **WHEN** 扫描已开始、挂单提交之前 scope 被冻结
- **THEN** 这次信号记 `BLOCKED`，不挂单

#### Scenario: 撤掉未成交单
- **WHEN** 运行 `freeze --cancel-working`
- **THEN** 该 scope 纸面运行里未成交的入场单变为 `CANCELLED`，已有仓位照常管理

### Requirement: 回滚
`rollback` SHALL 针对一个四维 scope，在发布库的一个事务里把 active 指针指回清单登记的上一已批准纸面版本并记审计。没有上一版本时 SHALL 冻结该 scope（原因 `RELEASE`）并停用指针的入场权限，保留旧记录和对存量仓位的管理。回滚 SHALL 需要与批准相同的人工确认。

#### Scenario: 无上一版本时冻结
- **WHEN** 对首次纸面基线回滚
- **THEN** 指针 `entries_enabled=0`，scope 冻结，原版本状态 `ROLLED_BACK`

#### Scenario: 旧仓回滚
- **WHEN** 回滚时新版本开的仓位还在
- **THEN** 仓位保留原版本元组并按原止损和截止规则完成，新信号使用回滚后的版本
