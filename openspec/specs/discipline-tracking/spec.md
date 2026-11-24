# discipline-tracking Specification

## Purpose

把真实交易中的纪律问题记成带证据的事件，区分"是否按自己的计划做"和"是否符合策略"，证据不足时如实标为未知，并每日汇总成指标。

## Requirements

### Requirement: 纪律事件的类型与证据
系统 SHALL 记录这些类型的纪律事件：`COUNTERTREND_BEFORE_CONFIRMATION`、`RETRY_WITHOUT_NEW_SETUP`、`EARLY_EXIT`、`STOP_WIDENED`、`STOP_DELAYED`、`UNPLANNED`、`MISSING_EVIDENCE`。每个事件 MUST 带动作时点、关联的计划和成交标识以及证据。

#### Scenario: 无计划开仓
- **WHEN** 一笔开仓成交的关联状态为 `UNPLANNED`
- **THEN** 生成一条 `UNPLANNED` 事件，关联该成交

### Requirement: 计划执行与策略一致性分开
每个纪律事件 SHALL 独立记录 `plan_adherence`（`COMPLIANT`、`VIOLATION`、`UNKNOWN`）和 `strategy_alignment`（`ALIGNED`、`DEVIATES`、`UNKNOWN`）。前者比较真实动作与当时生效的个人计划，后者取自计划预检时对策略的比较。

#### Scenario: 计划合规但策略偏离
- **WHEN** 一笔交易完全按一份预检为 `DEVIATES` 的计划执行
- **THEN** `plan_adherence` 为 `COMPLIANT`，`strategy_alignment` 为 `DEVIATES`

### Requirement: 拖止损与放宽止损需要订单时间证据
判定 `STOP_DELAYED` 或 `STOP_WIDENED` SHALL 依据带时间的止损提交、修改、触发或成交记录。已生效的止损因跳空或滑点成交得更差 MUST NOT 被认定为人为延迟。没有订单事件时 MUST 记为 `MISSING_EVIDENCE` 且 `plan_adherence` 为 `UNKNOWN`。

#### Scenario: 止损单被改远
- **WHEN** 订单事件显示多仓的止损单从 4995 被改到 4990
- **THEN** 生成 `STOP_WIDENED`，`plan_adherence` 为 `VIOLATION`

#### Scenario: 用市价单在止损之外离场
- **WHEN** 计划止损 4995，订单事件显示以市价单在 4990 平仓，没有止损单成交
- **THEN** 生成 `STOP_DELAYED`

#### Scenario: 止损单跳空成交
- **WHEN** 计划止损 4995，订单事件显示止损单在 4990 成交
- **THEN** 不生成 `STOP_DELAYED`

#### Scenario: 只有成交没有订单事件
- **WHEN** 计划止损 4995，平仓价 4990，没有任何订单事件
- **THEN** 生成 `MISSING_EVIDENCE`，`plan_adherence` 为 `UNKNOWN`

### Requirement: 提前离场
平仓价严格位于计划的止损与目标之间，且订单事件显示这笔平仓不是止损单或目标单成交时，系统 SHALL 生成 `EARLY_EXIT`。

#### Scenario: 盈利但提前走
- **WHEN** 多单计划目标 5010，订单事件显示以市价单在 5004 平仓
- **THEN** 生成 `EARLY_EXIT`，`plan_adherence` 为 `VIOLATION`

### Requirement: 盈亏不改变合规判定
盈利的违规 SHALL 仍记为违规。按计划的止损出场 SHALL 记为合规。

#### Scenario: 按计划止损
- **WHEN** 止损单在计划的止损价成交
- **THEN** 没有违规事件，该笔的 `plan_adherence` 为 `COMPLIANT`

### Requirement: 交易员纠正
系统 SHALL 保存交易员对某个事件的纠正，关联到该事件。纠正 MUST 只作为复盘证据，MUST NOT 改变任何规则、状态或已有的记录。

#### Scenario: 记录纠正
- **WHEN** 交易员对一条扫描记录写下纠正
- **THEN** 纠正被保存并关联到该记录，该记录本身不变

### Requirement: 计划覆盖率
系统 SHALL 每日为每个账户计算 `plan_coverage`：有事前已激活计划关联的开仓数除以全部开仓数，隔离的成交不计。低于配置的阈值时 MUST 标注样本不足。

#### Scenario: 四笔开仓三笔有计划
- **WHEN** 当日 4 笔开仓中 3 笔关联了事前计划，阈值为 0.8
- **THEN** `plan_coverage` 为 0.75，并标注样本不足

#### Scenario: 没有开仓
- **WHEN** 当日没有开仓
- **THEN** `plan_coverage` 为空

### Requirement: 提醒与纪律的每日指标
每日指标 SHALL 包含每类提醒的次数、从触发到解除的时长、到当日结束仍未解除的比例，以及每类纪律事件的次数。可判定与未知的数量 MUST 分开列出。

#### Scenario: 分列未知
- **WHEN** 当日 5 笔已平仓交易中 2 笔因缺少订单事件无法判定
- **THEN** 指标中可判定数为 3，未知数为 2
