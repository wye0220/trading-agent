# manual-plan-precheck Specification

## Purpose

让交易员在下单之前登记计划并得到一份预检：计划是否符合当前策略、有哪些纪律问题、账户风险是否允许。预检只给结论，不替交易员下单。

## Requirements

### Requirement: 登记计划
`plan` SHALL 接受账户、策略家族、实际合约、方向、入场、止损、目标、手数、理由和失效时间，生成唯一的 `plan_id`，记录提交时刻和预检时的版本。新计划的状态为 `DRAFT`。

#### Scenario: 登记
- **WHEN** 提交一份字段齐全的计划
- **THEN** 返回 `plan_id`，计划状态为 `DRAFT`，没有预留任何预算

### Requirement: 一行快捷语法
`plan` SHALL 接受"根代码 方向 入场 止损 目标 手数 理由"的一行写法，把根代码解析为当日实际合约并在输出中回显。账户和失效时间取配置的默认值。

#### Scenario: 正确解析
- **WHEN** 输入 `plan ES L 5000 4995 5010 1 PA Spike H1`
- **THEN** 计划为做多、入场 5000、止损 4995、目标 5010、1 手，理由为"PA Spike H1"，输出回显解析出的实际合约

#### Scenario: 字段缺失
- **WHEN** 输入 `plan ES L 5000`
- **THEN** 命令拒绝并指出缺少哪些字段，不生成计划

#### Scenario: 价格不在最小变动价位上
- **WHEN** 入场价写成 5000.10
- **THEN** 命令拒绝并说明原因

### Requirement: 预检的三部分
预检 SHALL 分别给出策略一致性（`ALIGNED`、`DEVIATES` 或 `UNKNOWN`）、账户风险状态及原因、纪律标记，并合成一个结论：风险不通过为 `BLOCKED_BY_RISK`；否则一致性为 `ALIGNED` 时为 `ALIGNED`；其余为 `DEVIATES`。

#### Scenario: 逆势
- **WHEN** 当前处于熊 Spike，计划做多，账户风险允许
- **THEN** 结论为 `DEVIATES`，纪律标记含 `COUNTERTREND_BEFORE_CONFIRMATION`

#### Scenario: 风险不允许
- **WHEN** 账户限额没有批准
- **THEN** 结论为 `BLOCKED_BY_RISK`，原因为 `BUDGET_NOT_APPROVED`

#### Scenario: 没有策略状态
- **WHEN** 该 scope 没有可用的策略状态，账户风险允许
- **THEN** `strategy_alignment` 为 `UNKNOWN`，结论为 `DEVIATES`

#### Scenario: 没有止损
- **WHEN** 计划没有填写止损
- **THEN** 纪律标记含 `NO_STOP`

### Requirement: 无新形态的重试
账户当日最近一次已完结交易是止损出场，而新计划与它同合约同方向且没有对应一个新的 setup 时，预检 SHALL 标记 `RETRY_WITHOUT_NEW_SETUP`。

#### Scenario: 止损后原样再来
- **WHEN** 多单刚止损，又提交一份同合约的做多计划，当前没有新的有效信号
- **THEN** 纪律标记含 `RETRY_WITHOUT_NEW_SETUP`

### Requirement: 激活才预留预算
计划 SHALL 只有在被显式激活时才预留风险预算。激活 MUST 重新预检，风险不通过时计划保持 `DRAFT`。

#### Scenario: 激活成功
- **WHEN** 激活一份风险允许的计划
- **THEN** 计划转为 `ACTIVE`，账户的已预留风险增加

#### Scenario: 激活被拦截
- **WHEN** 激活一份会使当日风险超限的计划
- **THEN** 计划保持 `DRAFT`，账户的已预留风险不变

### Requirement: 过期与取消释放预留
到失效时间仍未成交的计划 SHALL 转为 `EXPIRED`。取消的计划转为 `CANCELLED`。两者 MUST 释放已预留的风险。

#### Scenario: 过期
- **WHEN** 一份 `ACTIVE` 计划过了失效时间仍未成交
- **THEN** 它转为 `EXPIRED`，预留被释放

### Requirement: 状态变化留审计
计划的每次状态变化 SHALL 追加一条审计记录，含前后状态和时刻。更新 MUST 带版本校验。

#### Scenario: 审计轨迹
- **WHEN** 一份计划经历登记、激活、取消
- **THEN** 审计记录依次有这三次变化

### Requirement: 预检不下单
预检与激活 MUST NOT 向任何券商提交、修改或撤销订单。

#### Scenario: 静态检查
- **WHEN** 运行测试套件
- **THEN** 有测试断言人工轨的代码不含下单、改单、撤单的调用
