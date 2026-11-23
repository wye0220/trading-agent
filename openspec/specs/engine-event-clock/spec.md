# engine-event-clock Specification

## Purpose

规定引擎如何对待时间：什么数据在什么时刻可用，订单何时激活、何时过期，以及逐步运行如何保证不回头、不重复。

## Requirements

### Requirement: 决策只用已可见的数据
在决策时点 t，引擎 SHALL 只使用收盘时刻不晚于 t 且可用时点不晚于 t 的 K 线，并使用当时可见的 revision。之后才到的数据或修订 MUST 只影响它到达之后的决策。

#### Scenario: 信号 K 尚不可见
- **WHEN** 信号 K 在 09:35:00 收盘、09:35:05 可用，引擎以 09:35:02 为决策时点运行
- **THEN** 这一步看不到这根 K，不会基于它产生信号

#### Scenario: 迟到的修订
- **WHEN** 一根已经被用来产生信号的 K 之后出现了新的 revision
- **THEN** 已产生的信号、订单和成交保持不变

### Requirement: 时钟只前进
每个运行 SHALL 记录一个水位。决策时点早于水位的步进 MUST 被拒绝。

#### Scenario: 回头运行
- **WHEN** 一个运行已经步进到 10:00，再以 09:50 为决策时点步进
- **THEN** 步进被拒绝，运行库没有变化

### Requirement: 订单的时间窗口
订单 SHALL 在决策时点激活，在信号 K 收盘时刻加有效根数乘周期时过期。激活之前的触价 MUST NOT 用于成交。决策时点不早于过期时刻时 MUST NOT 生成订单。

#### Scenario: 稍晚激活
- **WHEN** 09:35 收盘的信号在 09:35:02 被处理，有效 1 根
- **THEN** 订单 09:35:02 激活，09:40:00 过期

#### Scenario: 激活前的触价
- **WHEN** 订单 09:35:10 激活，行情显示 09:35:01 曾触及触发价，之后有效期内没有再触及
- **THEN** 订单不成交，到期后为 `EXPIRED`

#### Scenario: 信号来得太晚
- **WHEN** 09:35 收盘的信号直到 09:40:30 才可见
- **THEN** 不生成订单，记录 `MISSED_LATE_SIGNAL`

### Requirement: 跨着激活时刻的行情要标记
用于成交的一根 K 的时间区间包含激活时刻时，由它产生的入场成交 SHALL 带 `AMBIGUOUS_ENTRY` 标记。

#### Scenario: 只有 5 分钟线
- **WHEN** 订单在 09:35:05 激活，可用的只有 09:35–09:40 这根 5 分钟线，它触及了触发价
- **THEN** 订单成交，成交带 `AMBIGUOUS_ENTRY`

#### Scenario: 完全在激活之后的 1 分钟线
- **WHEN** 订单在 09:35:05 激活，09:36–09:37 的 1 分钟线触及了触发价
- **THEN** 订单成交，不带 `AMBIGUOUS_ENTRY`

### Requirement: 逐步运行是幂等的
对同一个运行和同一根信号 K 重复步进 SHALL 不产生新的扫描记录、订单、成交或结算，并返回与第一次相同的结果。

#### Scenario: 重复步进
- **WHEN** 同一步被执行两次
- **THEN** 订单、成交、已完结交易和扫描记录的数量与内容都与执行一次相同

#### Scenario: 整段重放
- **WHEN** 用同样的数据和配置在两个不同的运行里各回放一遍
- **THEN** 两次的逐笔成交和逐条决策完全一致

### Requirement: 提交订单前重读冻结状态
生成订单的事务 SHALL 在提交前重读该 scope 的冻结状态和数据阻止状态。处于冻结或阻止时 MUST NOT 生成订单，并记录原因。

#### Scenario: 冻结与入场同刻
- **WHEN** 策略给出 `SETUP_VALID`，而该 scope 在这一步提交之前被冻结
- **THEN** 不生成订单，扫描记录的 `risk_status` 为 `BLOCKED`，原因指明冻结

### Requirement: 引擎不依赖具体策略
引擎 SHALL 只通过策略插件接口与策略交互。一个与 PA 无关、使用另一种订单类型的策略 MUST 能跑通同样的流程。

#### Scenario: 模拟策略
- **WHEN** 用一个每根都发 `MARKET_NEXT_OPEN` 加 `INITIAL_RISK_MULTIPLE` 的模拟策略回放一段合成数据
- **THEN** 产生订单、成交和已完结交易，过程中没有用到任何 PA 专有的字段

### Requirement: 无法定价的判定时点
运行器 SHALL 只在 RTH 收盘再过配置的宽限时间（`unpriceable_grace_seconds`）之后、仓位仍未平掉时，才把仓位标为 `UNPRICEABLE`、`UNABLE_TO_FLATTEN` 并写告警。截止强平时刻到收盘之间，平仓用的子 K 尚未到达不算没有行情。

#### Scenario: 回放中的截止强平
- **WHEN** 仓位持有到截止强平时刻，下一步平仓用的子 K 到达并平仓
- **THEN** 交易的退出原因为 `FORCED_FLAT`，不带 `UNPRICEABLE` 或 `UNABLE_TO_FLATTEN`，没有告警

#### Scenario: 截止之后确实没有行情
- **WHEN** 截止强平时刻之后没有任何子 K，决策时点到达收盘加宽限时间
- **THEN** 仓位被标为 `UNPRICEABLE` 和 `UNABLE_TO_FLATTEN`，告警只写一次
