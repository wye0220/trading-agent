# release-shadow Specification

## Purpose

规定通过正式确认的候选进入正式影子：计划在登记时冻结，两臂以同一决策时点读同一批后续行情、各写各的账本，到预登记终点才判定运行验收结果；以及队列前几名的实时前向预演和前向保留窗口的输入排除。

## Requirements

### Requirement: 正式影子只接收通过正式确认的候选
`shadow --register` SHALL 只接受状态为 `PASS_FOR_SHADOW` 的候选。登记 SHALL 冻结影子计划：起始交易日、有效日数、每臂最低可核算成交数、检查项、窗口性质（运行验收或前向保留）和可选的非劣效设置，计划之后不可修改。起始日 SHALL 晚于运行库里已有的最后一个交易日。

#### Scenario: 未通过正式确认
- **WHEN** 对状态不是 `PASS_FOR_SHADOW` 的候选登记影子
- **THEN** 拒绝，原因 `NOT_PASS_FOR_SHADOW`

#### Scenario: 已披露日期复用
- **WHEN** 影子计划的起始日不晚于运行库里已有数据的最后一个交易日
- **THEN** 拒绝，原因 `DISCLOSED_DATES`

### Requirement: 两臂读同一批行情、各写各的账本
影子 SHALL 建两个 `live_shadow` 运行：基线臂用当前 active 版本，候选臂用 active 版本加候选差异。每次步进 SHALL 以同一决策时点推进两臂。两臂 SHALL 受冻结约束。

#### Scenario: 同一时点
- **WHEN** 运行 `shadow --step`
- **THEN** 两臂的扫描记录有相同的决策时点，订单和成交分别记在各自的运行下

### Requirement: 到预登记终点才判定
`shadow --finish` SHALL 只在计划的有效日数走完之后运行；之前 SHALL 拒绝，且没有延长窗口的入口。判定 SHALL 检查数据完整性、订单与账本一致、持仓日终归零、冻结期间没有新入场、两臂成交数达标；启用非劣效时 SHALL 用预登记的界值、α 和区块 bootstrap 检验配对净 R／日的单侧下界。结果为 `PASSED`、`REJECTED` 或 `EVIDENCE_INSUFFICIENT`，收益只作描述。

#### Scenario: 提前查看
- **WHEN** 计划的有效日数没走完就运行 `shadow --finish`
- **THEN** 拒绝，原因 `NOT_AT_ENDPOINT`

#### Scenario: 样本不足
- **WHEN** 走完计划后某一臂的可核算成交少于最低数
- **THEN** 结果 `EVIDENCE_INSUFFICIENT`，候选不能批准

#### Scenario: 运行检查不过
- **WHEN** 影子期间出现引擎异常标记或账本不一致
- **THEN** 结果 `REJECTED`，候选状态 `REJECTED`

### Requirement: 前向保留窗口不进作者的输入
窗口性质为前向保留时，在影子结束前 SHALL 把窗口内的交易日排除在日复盘、聚合和候选修改的输入之外。

#### Scenario: 影子期间跑聚合
- **WHEN** 前向保留窗口未结束时运行 `evolve`
- **THEN** 窗口内的 session 计入排除，原因 `FORWARD_HOLDOUT`

### Requirement: 实时前向预演
`shadow --step` SHALL 同时为各 scope 队列前三名候选推进 `pre_eval_shadow` 运行，只记录可运行性和引擎异常，不报告盈亏；看过的日期 SHALL 记入 `pre_eval_shadow_days`。

#### Scenario: 预演日期
- **WHEN** 一个候选做过实时前向预演
- **THEN** 那些日期出现在它的 `pre_eval_shadow_days` 里
