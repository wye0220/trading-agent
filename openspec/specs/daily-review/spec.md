# daily-review Specification

## Purpose

对已封存的 session 做规则生成的日复盘：把当时知道的事实、证据不足的判断、事后信息和改动假设分开写，给每笔交易归因，并产出带 scope、机制标签和证据的观察，供跨日聚合使用。复盘不调用模型，同一输入结果相同；也支持只复盘人工轨。

## Requirements

### Requirement: 只复盘已封存的 session
`review` SHALL 只读取已封存的 session、当时的版本元组、`trader_plans`、`manual_trades`、`manual_order_events`、`trader_notes` 和开发可见的行情。未封存或受保护的 session MUST 被拒绝。

#### Scenario: 未封存
- **WHEN** 对一个未封存的 session 运行复盘
- **THEN** 命令拒绝，不产生任何文件

### Requirement: 输出通过 schema
复盘 SHALL 输出 `reports/sessions/<session_id>.md` 和 `.json`。JSON 中的 `review` 对象 MUST 通过 `SessionReview` schema，否则不写文件并登记校验失败。

#### Scenario: 相同输入
- **WHEN** 对同一个 session 复盘两次
- **THEN** 两次的 `review` 对象相同

### Requirement: 四类信息分开
复盘 SHALL 把内容分成四类：当时知道的事实、证据不足的判断、事后信息、改动假设。事后才知道的量（如 MFE、过期后价格是否到过目标）MUST 只出现在事后信息里。

#### Scenario: 过期的订单
- **WHEN** 一张挂单到期未成交，之后价格越过了它的触发价
- **THEN** "订单到期"写在当时知道的事实里，"之后价格越过触发价"写在事后信息里

### Requirement: 机会与数据
复盘 SHALL 列出当日规则允许的机会和被拦截的机会，各附决策记录的 `event_id`，并给出数据是否完整、所用子 K 周期和数据异常。

#### Scenario: 被拦截的机会
- **WHEN** 策略给出信号但因已两次止损而没有挂单
- **THEN** 该机会列在"被拦截"里，带原因和 `event_id`

### Requirement: 策略轨
复盘 SHALL 按 scope 和 `config/research.yaml` 登记的元数据分组，记录允许与过滤的数量、净 R、MAE／MFE、到期未成交的订单和成交歧义。分组键 MUST 来自配置，复盘代码不含策略专有字段名。

#### Scenario: 分组
- **WHEN** 当日有两笔不同编号的交易
- **THEN** 策略轨按登记的分组分别给出笔数和净 R

### Requirement: 人工轨
指定账户时，复盘 SHALL 先报 `plan_coverage`，再分别评价计划执行和策略一致性，列出纪律事件，并逐条回顾当日的 `watch` 提醒（触发原因、处理时长、提醒后是否再犯）。证据不足的标 `UNKNOWN`，纪律比例的分母只含可判定的动作，未知数量单列。`plan_coverage` 低于阈值时纪律结论 MUST 标注样本不足。

#### Scenario: 覆盖率不足
- **WHEN** 当日 4 次开仓里只有 1 次有事前计划
- **THEN** 复盘先报 `plan_coverage` 为 25%，并标注样本不足

#### Scenario: 没有人工成交
- **WHEN** 该账户当日没有成交
- **THEN** 人工轨写明没有成交，`plan_coverage` 为空

### Requirement: 归因
每笔纸面交易、每笔人工平仓和每个数据或引擎异常 SHALL 归入且只归入一类：`DATA_ENGINE_ERROR`、`RULE_EXECUTION_DEVIATION`、`LOSS_BY_RULE`、`PROFIT_BY_RULE`、`OUT_OF_RULE_OBSERVATION`。盈利的违规仍归为执行偏差；按计划止损归为按规则亏损。

#### Scenario: 盈利的违规
- **WHEN** 一笔人工平仓盈利但被判为违反计划
- **THEN** 它归入 `RULE_EXECUTION_DEVIATION`

#### Scenario: 按计划止损
- **WHEN** 一笔人工交易在计划止损位止损出场且判为合规
- **THEN** 它归入 `LOSS_BY_RULE`

### Requirement: 回应纠正
当日关联的每条 `trader_notes` SHALL 得到一条回应：`ACCEPT`、`REBUT`（附证据）或 `PENDING`。规则无法判定的 MUST 标 `PENDING`，不得臆断。

#### Scenario: 无法判定
- **WHEN** 一条备注的内容不属于可由规则核对的类型
- **THEN** 回应为 `PENDING`

### Requirement: 观察与候选
每条观察 SHALL 是带四维 scope、机制标签和证据 `event_id` 的 `OBSERVATION`。只有指向数据或引擎问题的观察可以直接产出 `PROPOSED` 候选（类别 `data_engine`）并登记台账；策略和纪律类 MUST 只登记为观察。没有可登记内容时输出 `NO_OBSERVATION` 并说明需要补什么记录。

#### Scenario: 策略类观察
- **WHEN** 当日两次止损后策略停手
- **THEN** 登记一条观察，不产生候选

#### Scenario: 数据问题
- **WHEN** 当日有 `DATA_ERROR`
- **THEN** 登记观察，并产出一个类别为 `data_engine` 的 `PROPOSED` 候选

#### Scenario: 平静的一天
- **WHEN** 当日没有交易、没有拦截、没有异常
- **THEN** 输出一条 `NO_OBSERVATION`

### Requirement: Memory 候选
只有备注里用登记的格式明确表达的偏好（白名单键值）SHALL 生成 `MemoryCandidate`，并通过其 schema。当前仓位、风险累计和风险上限 MUST NOT 进入 Memory 候选。

#### Scenario: 明确的偏好
- **WHEN** 备注为 `偏好: report_max_lines=5`
- **THEN** 生成一条 `user_preference` 类的 Memory 候选，状态待确认

#### Scenario: 试图写入风险上限
- **WHEN** 备注为 `偏好: max_contracts=10`
- **THEN** 不生成候选，该键不在白名单内

### Requirement: 只复盘人工轨
系统 SHALL 支持为一个账户的一个交易日单独封存和复盘人工轨，不需要策略运行，也不读取任何行情。这种 session 只含用户自己的计划、成交和提醒，因此不受开发可见截止日的限制。交易日结束之前 MUST NOT 封存。

#### Scenario: 截止日之后的真实操作
- **WHEN** 账户在晚于开发可见截止日的某天有成交，用只复盘人工轨的方式运行
- **THEN** 得到一份通过 schema 的复盘，其中没有策略轨内容，过程中没有读取任何 K 线

#### Scenario: 交易日还没结束
- **WHEN** 在该交易日结束之前请求封存
- **THEN** 封存被拒绝，原因是 `SESSION_IN_PROGRESS`
