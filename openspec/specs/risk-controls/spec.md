# risk-controls Specification

## Purpose

计算每笔交易的计划风险和手数，把纸面评测与真实账户两个风险域分开核算，并在真实账户一侧执行预算、日损和停手的限制。

## Requirements

### Requirement: 每手计划风险
每手计划风险 SHALL 等于止损 tick 数乘以 tick 价值，加每手往返费用，加滑点预算。费用按"每边"或"往返"二选一配置。

#### Scenario: ES 的例子
- **WHEN** ES 止损距离 16 tick，往返费用 5 美元，滑点预算 1 tick
- **THEN** 每手计划风险为 16 × 12.5 + 5 + 12.5 = 217.5 美元

#### Scenario: 每边计费
- **WHEN** 费用配置为每边 2.5 美元
- **THEN** 每手往返费用按 5 美元计算

### Requirement: 纸面评测固定手数
纸面运行 SHALL 固定 1 手，MUST NOT 经过账户预算门禁。

#### Scenario: 没有配置账户预算
- **WHEN** 账户预算没有配置或未批准，纸面运行出现有效信号
- **THEN** 仍然生成 1 手的纸面订单

### Requirement: 按预算算手数
人工计划的手数 SHALL 为单笔预算除以每手风险向下取整，再受最大手数和当日剩余风险约束。结果为 0 时 MUST 返回 `BLOCKED` 和 `MIN_SIZE_OVER_BUDGET`。

#### Scenario: 正常
- **WHEN** 单笔预算 500 美元，每手风险 217.5 美元，最大手数 5
- **THEN** 手数为 2

#### Scenario: 一手都超预算
- **WHEN** 单笔预算 200 美元，每手风险 217.5 美元
- **THEN** 手数为 0，结论 `BLOCKED`，原因 `MIN_SIZE_OVER_BUDGET`

#### Scenario: 受当日剩余风险约束
- **WHEN** 按单笔预算可开 2 手，但当日剩余风险只够 1 手
- **THEN** 手数为 1

### Requirement: 预算未批准时不放行
账户预算没有配置或没有标为已批准时，人工计划的风险核验 SHALL 返回 `BLOCKED` 和 `BUDGET_NOT_APPROVED`。

#### Scenario: 默认配置
- **WHEN** 使用未填写的风控配置核验一份人工计划
- **THEN** 结论 `BLOCKED`，原因 `BUDGET_NOT_APPROVED`

### Requirement: 真实账户的限制跨策略共享
真实账户的日损、已预留风险和两次止损停手 SHALL 以账户和交易日为键，在所有策略之间共享。停手之后，任何策略的新入场、激活和预留 MUST 被拦截。达到两次之前，只有已完结的净盈利交易能清零。

#### Scenario: 换策略不解除停手
- **WHEN** 账户当日已有两次止损，随后用另一个策略家族的计划请求核验
- **THEN** 结论 `BLOCKED`，原因指明账户停手

#### Scenario: 同一仓位只计一次
- **WHEN** 同一笔止损成交被重复导入
- **THEN** 止损次数只增加一次

#### Scenario: 日损到限
- **WHEN** 账户当日已实现亏损达到日损上限
- **THEN** 新的计划被拦截，原因 `DAILY_LIMIT_HIT`

### Requirement: 核验与预留在同一事务
风险的核验和预留 SHALL 在同一个事务里完成，并带版本校验，使并发的两份计划不会共同超出预算。

#### Scenario: 并发的两份计划
- **WHEN** 剩余风险只够一份计划，两份计划先后请求预留
- **THEN** 第一份成功，第二份被拦截

#### Scenario: 过期的账户状态
- **WHEN** 预留时携带的账户状态版本已经过期
- **THEN** 预留被拒绝，账户状态不变

### Requirement: 账户状态未知时拦截
真实仓位、止损配置、订单状态或成交数据缺失或过期时，人工计划的核验 SHALL 返回 `BLOCKED` 和 `RISK_UNKNOWN_OR_STALE`。

#### Scenario: 持仓数据过期
- **WHEN** 最近一次账户快照早于允许的时效
- **THEN** 结论 `BLOCKED`，原因 `RISK_UNKNOWN_OR_STALE`

### Requirement: 两个风险域隔离
纸面的盈亏和止损计数 SHALL 以 scope、运行和交易日为键，MUST NOT 影响真实账户的限制，反之亦然。

#### Scenario: 纸面止损不影响账户
- **WHEN** 一个纸面运行当日止损两次
- **THEN** 真实账户的止损次数不变，人工计划的核验不受影响

### Requirement: R 的计量
每笔已完结的纸面交易 SHALL 记录按初始风险归一化的 R。配置了固定研究风险单位时，还 SHALL 记录按该单位计的 R；没有配置时 MUST 带 `RESEARCH_RISK_UNIT_NOT_SET` 标记。费用没有配置时按 0 计并带 `FEES_NOT_CONFIGURED` 标记。

#### Scenario: 配置了研究风险单位
- **WHEN** 研究风险单位为 200 美元，一笔交易净盈利 400 美元
- **THEN** 该笔按研究单位计为 2R

#### Scenario: 费用未配置
- **WHEN** 费用留空
- **THEN** 交易结果的费用为 0，带 `FEES_NOT_CONFIGURED`

#### Scenario: 跳空造成超计划亏损
- **WHEN** 一笔交易因跳空在止损之外平仓
- **THEN** 按初始风险计的 R 小于 −1，并带 `GAP_THROUGH_STOP`

### Requirement: 账户限额按账户配置
系统 SHALL 从 `config/accounts.yaml` 读取每个账户的限额、费用和滑点预算，用于该账户的人工计划预检、`watch` 和成交导入。未登记的账户沿用 `config/risk.yaml` 的账户设置。账户限额 MUST NOT 影响纸面评测的风控版本和基线。

#### Scenario: 不改变纸面评测
- **WHEN** 修改某个账户的限额
- **THEN** `config/risk.yaml` 的内容和由它得出的 `risk_version` 不变

#### Scenario: 未批准的账户
- **WHEN** 账户的 `approved` 为 false
- **THEN** 它的人工计划预检结论为 `BLOCKED_BY_RISK`，原因 `BUDGET_NOT_APPROVED`
