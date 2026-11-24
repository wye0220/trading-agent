## ADDED Requirements

### Requirement: 无法定价的判定时点
运行器 SHALL 只在 RTH 收盘再过配置的宽限时间（`unpriceable_grace_seconds`）之后、仓位仍未平掉时，才把仓位标为 `UNPRICEABLE`、`UNABLE_TO_FLATTEN` 并写告警。截止强平时刻到收盘之间，平仓用的子 K 尚未到达不算没有行情。

#### Scenario: 回放中的截止强平
- **WHEN** 仓位持有到截止强平时刻，下一步平仓用的子 K 到达并平仓
- **THEN** 交易的退出原因为 `FORCED_FLAT`，不带 `UNPRICEABLE` 或 `UNABLE_TO_FLATTEN`，没有告警

#### Scenario: 截止之后确实没有行情
- **WHEN** 截止强平时刻之后没有任何子 K，决策时点到达收盘加宽限时间
- **THEN** 仓位被标为 `UNPRICEABLE` 和 `UNABLE_TO_FLATTEN`，告警只写一次
