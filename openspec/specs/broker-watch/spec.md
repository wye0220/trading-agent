# broker-watch Specification

## Purpose

在盘中按固定间隔只读地查看真实账户的持仓和挂单，发现违反纪律的情形时即时提醒。提醒只陈述事实和对应的规则，不给交易指令。

## Requirements

### Requirement: 只读的账户快照
`watch` SHALL 通过只读适配器取得账户的持仓、挂单和当日成交，并把每次快照连同取数时刻和来源保存。适配器 MUST NOT 含有下单、改单或撤单的调用。

#### Scenario: 保存快照
- **WHEN** 完成一次采集
- **THEN** 新增一条账户快照，含取数时刻、来源和内容摘要

#### Scenario: 适配器只读
- **WHEN** 运行测试套件
- **THEN** 有测试断言适配器的接口只有读取方法，源码不含下单、改单、撤单的调用

### Requirement: 可调的轮询间隔
轮询间隔 SHALL 取自配置，默认 60 秒，并可由命令行覆盖为 300 秒或任意正整数秒。

#### Scenario: 默认间隔
- **WHEN** 不带参数运行 `watch`
- **THEN** 间隔为 60 秒

#### Scenario: 切到 5 分钟
- **WHEN** 以 `--interval 300` 运行
- **THEN** 间隔为 300 秒，宽限时间和过期阈值随之调整

### Requirement: 数据过期时不推断
快照的取数时刻早于 max(2 × 间隔, 120) 秒之前，或读取失败时，`watch` SHALL 发出告警并标明数据时点，MUST NOT 评估任何纪律规则。

#### Scenario: 读取失败
- **WHEN** 适配器读取失败
- **THEN** 产生一条数据过期告警，没有任何纪律提醒

#### Scenario: 阈值随间隔变化
- **WHEN** 间隔为 300 秒，快照是 400 秒前的
- **THEN** 快照未过期，规则照常评估

### Requirement: 裸仓提醒
有持仓而没有对应的保护止损单、且持续时间超过宽限时间时，`watch` SHALL 发出 `NAKED_POSITION` 提醒。宽限时间默认等于轮询间隔。

#### Scenario: 超过宽限
- **WHEN** 连续两次快照都有持仓而没有止损单，两次相隔超过宽限时间
- **THEN** 发出 `NAKED_POSITION`

#### Scenario: 宽限内补上了止损
- **WHEN** 第一次快照没有止损单，宽限时间内的下一次快照有了
- **THEN** 不发出提醒

### Requirement: 止损被移远或撤掉
同一张止损单的价格相对上一快照向不利方向移动，或止损单消失而持仓仍在时，`watch` SHALL 发出 `STOP_MOVED_AWAY` 提醒。

#### Scenario: 多仓的止损被下移
- **WHEN** 多仓的止损单从 4995 被改到 4990
- **THEN** 发出 `STOP_MOVED_AWAY`

#### Scenario: 止损上移不算
- **WHEN** 多仓的止损单从 4995 被改到 4998
- **THEN** 不发出提醒

### Requirement: 冷却期内再入场
止损出场后的冷却时间内同方向再次开仓时，`watch` SHALL 发出 `REENTRY_IN_COOLDOWN` 提醒，并在证据中列出是否有新的 setup、是否有已激活的计划、账户是否已停手。

#### Scenario: 止损后立刻同向开仓
- **WHEN** 多单止损 5 分钟后又出现多仓，冷却时间为 15 分钟
- **THEN** 发出 `REENTRY_IN_COOLDOWN`

### Requirement: 无计划开仓
新出现的持仓找不到相符的已激活计划时，`watch` SHALL 发出 `UNPLANNED_ENTRY` 提醒。

#### Scenario: 有计划
- **WHEN** 新持仓与一份已激活的计划账户、合约、方向相符
- **THEN** 不发出提醒

#### Scenario: 没有计划
- **WHEN** 新持仓没有相符的已激活计划
- **THEN** 发出 `UNPLANNED_ENTRY`

### Requirement: 逆势加仓
持仓增加而策略对该方向的一致性评估为逆势时，`watch` SHALL 发出 `COUNTERTREND_ADD` 提醒。没有可用的策略状态时 MUST NOT 发出，并把该次判定记为未知。

#### Scenario: 熊 Spike 中加多
- **WHEN** 策略状态为熊 Spike，多仓从 1 手增加到 2 手
- **THEN** 发出 `COUNTERTREND_ADD`

#### Scenario: 没有策略状态
- **WHEN** 没有可用的策略状态，持仓增加
- **THEN** 不发出提醒

### Requirement: 接近日损上限
账户当日已实现加未实现亏损达到日损上限的 80% 时，`watch` SHALL 发出 `DAILY_LOSS_NEAR_LIMIT` 提醒。日损上限没有配置或快照没有未实现盈亏时不评估。

#### Scenario: 达到 80%
- **WHEN** 日损上限 1000 美元，已实现亏损 500 美元，未实现亏损 320 美元
- **THEN** 发出 `DAILY_LOSS_NEAR_LIMIT`

### Requirement: 提醒的记录与去重
每条提醒 SHALL 记录规则、账户、快照标识和关联的计划，文字只陈述事实和规则。同一持仓同一规则在静默窗口内 MUST NOT 重复发出。条件消失时 SHALL 记录解除时刻。

#### Scenario: 静默窗口内不重复
- **WHEN** 裸仓状态持续，静默窗口内又采集了三次
- **THEN** 只有一条 `NAKED_POSITION` 提醒

#### Scenario: 条件解除
- **WHEN** 之后的快照里出现了保护止损单
- **THEN** 那条提醒被标为已解除并记录时刻

### Requirement: 心跳
`watch` SHALL 在每轮采集后持久化心跳时刻，供外部判断它是否仍在运行。

#### Scenario: 记录心跳
- **WHEN** 完成一轮采集（无论成功与否）
- **THEN** 心跳时刻被更新
