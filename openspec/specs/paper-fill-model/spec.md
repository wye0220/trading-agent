# paper-fill-model Specification

## Purpose

用一套带版本的规则模拟纸面订单的触发、成交和退出，对无法确定先后的情形取保守结果并打标记，绝不凭空造出成交。

## Requirements

### Requirement: 止损限价单的状态机
`STOP_LIMIT_NEXT_BAR` 订单 SHALL 按 `ARMED → TRIGGERED → FILLED` 或 `EXPIRED` 推进。价格到达触发价即触发。只有存在限价或更优价格的证据时才成交，成交价 MUST NOT 越过限价。

#### Scenario: 盘中触发并成交
- **WHEN** 买单触发价 5000、限价 5001，激活后的一根 K 开盘 4999、最高 5002
- **THEN** 订单在 5000 成交

#### Scenario: 跳空越过限价
- **WHEN** 买单触发价 5000、限价 5001，下一根开盘 5003，最低 5002
- **THEN** 订单为 `TRIGGERED`，没有成交

#### Scenario: 之后回到限价
- **WHEN** 上述订单在有效期内的后一根 K 最低 5000.5
- **THEN** 订单在 5001 成交

#### Scenario: 一直没有回到限价
- **WHEN** 上述订单直到过期价格都高于 5001
- **THEN** 订单为 `EXPIRED`，没有成交

#### Scenario: 跳空但在限价之内
- **WHEN** 买单触发价 5000、限价 5001，下一根开盘 5000.75
- **THEN** 订单在 5000.75 成交

#### Scenario: 没有触及
- **WHEN** 有效期内最高价始终低于 5000
- **THEN** 订单为 `EXPIRED`

### Requirement: 其他订单类型
`MARKET_NEXT_OPEN` SHALL 在激活后第一个可用的开盘价成交。`LIMIT_RETRACE` SHALL 在有效期内价格到达限价时按限价成交。

#### Scenario: 下一个开盘
- **WHEN** `MARKET_NEXT_OPEN` 买单 09:35:05 激活，09:36 开始的 1 分钟线开盘 5000.25
- **THEN** 订单在 5000.25 成交

#### Scenario: 回落限价
- **WHEN** `LIMIT_RETRACE` 买单限价 4998，有效期内一根 K 最低 4997.5
- **THEN** 订单在 4998 成交

### Requirement: 成交后的保护
订单成交后，保护止损和目标 SHALL 立即生效，其中一个成交时另一个撤销。目标 SHALL 由三种通用描述在成交后换算成价格。

#### Scenario: 偏移目标
- **WHEN** 买单在 5000.25 成交，目标为成交价加 81 tick，tick 为 0.25
- **THEN** 目标价为 5020.50

#### Scenario: 风险倍数目标
- **WHEN** 买单在 5000 成交，止损 4996，目标为初始风险的 2 倍
- **THEN** 目标价为 5008

#### Scenario: 到达目标
- **WHEN** 持有多仓，后面一根 K 最高价到达目标价而没有触及止损
- **THEN** 仓位在目标价平仓，退出原因为 `TARGET`

#### Scenario: 触及止损
- **WHEN** 持有多仓，后面一根 K 最低价触及止损而没有到达目标
- **THEN** 仓位按止损价加不利滑点平仓，退出原因为 `STOP`

### Requirement: 跳空穿过止损
已有保护止损遇到开盘跳空越过止损价时，仓位 SHALL 按开盘价加不利滑点平仓，并带 `GAP_THROUGH_STOP` 标记。

#### Scenario: 跳空低开
- **WHEN** 多仓止损 4996，下一根开盘 4990
- **THEN** 仓位在 4990 减去滑点处平仓，标记 `GAP_THROUGH_STOP`，亏损超过计划风险

### Requirement: 先后不明时止损优先
一根 K 内止损和目标都可能触及而无法确定先后时，引擎 SHALL 按止损处理并带 `AMBIGUOUS_BAR` 标记。入场当根触及止损时同样按止损处理并带该标记。先后不明的触价 MUST NOT 被记为盈利。

#### Scenario: 同根触及止损和目标
- **WHEN** 多仓止损 4996、目标 5008，一根 K 最低 4995、最高 5009
- **THEN** 仓位按止损平仓，标记 `AMBIGUOUS_BAR`

#### Scenario: 入场当根触及止损
- **WHEN** 买单在一根 K 内于 5000 成交，同一根 K 的最低价低于止损
- **THEN** 仓位按止损平仓，标记 `AMBIGUOUS_BAR`

#### Scenario: 跳空触发、回落成交、同根穿损
- **WHEN** 买单触发价 5000、限价 5001、止损 4996，一根 K 开盘 5003、最低 4995
- **THEN** 订单在 5001 成交，随即按止损平仓，标记 `AMBIGUOUS_BAR`

#### Scenario: 回落成交当根的最高价不算到达目标
- **WHEN** 买单触发价 5000、限价 5001，一根 K 开盘 5010、最低 5000.5、最高 5010，目标 5009，止损未触及
- **THEN** 订单在 5001 成交，仓位保持持有，标记 `AMBIGUOUS_PATH`

### Requirement: 用更细的行情定位先后
有 1 分钟线时，引擎 SHALL 用它逐根推进，使止损与目标落在不同分钟时先后是确定的。

#### Scenario: 先到目标后到止损价
- **WHEN** 多仓的目标在 10:01 的 1 分钟线触及，止损价在 10:03 的 1 分钟线才被触及
- **THEN** 仓位在目标价平仓，不带 `AMBIGUOUS_BAR`

### Requirement: 截止强平
到达截止时刻（RTH 收盘前 5 分钟，提前收盘日相应提前）时，引擎 SHALL 撤销未成交的入场单，并把持仓按截止时刻起第一个可用价格加不利滑点平仓，退出原因为 `FORCED_FLAT`。截止之后 MUST NOT 生成新订单。

#### Scenario: 常规日
- **WHEN** 15:55 ET 仍持有多仓
- **THEN** 仓位按 15:55 起第一个可用价格平仓，退出原因 `FORCED_FLAT`

#### Scenario: 半日市
- **WHEN** 当日 13:00 ET 收盘，12:55 ET 仍有未成交的入场单
- **THEN** 该订单被撤销

#### Scenario: 截止后没有行情
- **WHEN** 截止时刻之后没有可用的行情
- **THEN** 仓位标为无法定价并记录 `UNABLE_TO_FLATTEN`，等到第一个有效价格再平仓

### Requirement: 原子反手
持仓期间一张反向订单成交时，引擎 SHALL 在同一个模型价格上平掉旧仓并开出新仓，各自计费，旧仓退出原因为 `REVERSAL`。反向订单未成交时旧仓 MUST 按原计划管理。

#### Scenario: 反手成交
- **WHEN** 持有多仓，一张卖出的止损限价单在 4998 成交
- **THEN** 多仓在 4998 平仓，同时开出 4998 的空仓，产生两笔费用

#### Scenario: 反向订单过期
- **WHEN** 持有多仓，反向订单直到过期没有触发
- **THEN** 多仓的止损和目标不变

### Requirement: 缺失行情不造成交
有效期内没有行情的时段 MUST NOT 产生成交，也 MUST NOT 被插值。受影响的订单和仓位 SHALL 带数据缺口标记。

#### Scenario: 有效期内缺数据
- **WHEN** 订单的有效期内没有任何可用的 K
- **THEN** 订单到期为 `EXPIRED`，带 `DATA_GAP_IN_WINDOW`

### Requirement: 多空对称
成交模型对一组订单和行情的处理 SHALL 与对其价格翻转后的处理互为镜像。

#### Scenario: 镜像运行
- **WHEN** 把一个做多的成交场景关于某个价位翻转后运行
- **THEN** 事件的种类和顺序相同，方向相反，价格关于该价位对称

### Requirement: 成交模型带版本
成交规则和事件顺序 SHALL 有一个版本号，记入每张订单绑定的版本元组。

#### Scenario: 订单记录版本
- **WHEN** 生成一张纸面订单
- **THEN** 它绑定的版本元组里有引擎版本
