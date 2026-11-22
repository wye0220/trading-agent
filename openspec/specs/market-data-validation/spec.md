# market-data-validation Specification

## Purpose

在数据被用于决策之前发现并分类行情异常，异常时阻止受影响 scope 的新入场，保证系统不在坏数据上产生信号或成交。

## Requirements

### Requirement: 校验项目
系统 SHALL 校验：实际合约与换月规则是否一致、时间轴是否符合交易日历、重复键与内容冲突、缺口、OHLC 一致性、提前收盘。每个异常 MUST 带类别、受影响的合约和时间范围。

#### Scenario: OHLC 不一致
- **WHEN** 一根 K 线的最高价低于开盘价
- **THEN** 该 K 线被记为 `OHLC_INCONSISTENT`，不入库

#### Scenario: 同一文件内重复键且内容冲突
- **WHEN** 同一文件中同一合约、同一起始时刻出现两条内容不同的记录
- **THEN** 两条都不入库，记为 `CONFLICTING_DUPLICATE`

#### Scenario: 交易时段外的 K 线
- **WHEN** 一根 K 线的时间落在日历标明的休市时段
- **THEN** 该 K 线被记为 `OUTSIDE_SESSION`

### Requirement: 合约与换月一致性
对给定 scope 和交易日，系统 SHALL 按版本化的换月规则确定实际合约。用于该 scope 的数据若来自其他合约，MUST 记为 `WRONG_CONTRACT`。

#### Scenario: 换月后仍使用旧合约
- **WHEN** 换月规则指定某交易日的实际合约为 `ESM2025`，而提供的 5 分钟线来自 `ESH2025`
- **THEN** 校验报告 `WRONG_CONTRACT`

### Requirement: 缺口识别
系统 SHALL 把交易时段内预期应有而没有的 K 线识别为缺口，并区分预期内的空白（休市、提前收盘之后）与非预期缺口。系统 MUST NOT 用插值或前值填充缺口。

#### Scenario: RTH 中间缺一根 5 分钟线
- **WHEN** 某 RTH 交易日 10:15 的 5 分钟线不存在
- **THEN** 校验报告一个非预期缺口，时间范围为 10:15–10:20 ET，库中不出现合成的 K 线

#### Scenario: 半日市
- **WHEN** 交易日历标明当天 13:00 ET 提前收盘
- **THEN** 13:00 ET 之后没有 RTH K 线不算缺口

#### Scenario: 夏令时切换日
- **WHEN** 校验美国夏令时开始或结束当周的数据
- **THEN** RTH 仍为纽约时间 09:30–16:00，不因 UTC 偏移变化而报告缺口或重复

### Requirement: 来源质量标记
来源附带逐日质量状态时，系统 SHALL 读取它，并把状态不是"可用"的交易日标为降级日。

#### Scenario: Databento 标记的降级日
- **WHEN** `condition.json` 将某日标为 `degraded`
- **THEN** 该交易日在校验结果和可得性报告中标为降级日

### Requirement: 数据异常阻止新入场
scope 当前数据存在非预期缺口、合约错误、冲突或新鲜度不足时，系统 SHALL 把该 scope 标为不可新入场，并给出原因。已有纸面仓位 MUST 保留并标为无法定价，直到首个可用行情到来。

#### Scenario: 最新 K 线缺失
- **WHEN** `scan` 运行时，scope 最近一根预期已收线的 5 分钟线不存在
- **THEN** 结果为 `DATA_ERROR`，该 scope 被标为不可新入场

#### Scenario: 采集停止导致数据过期
- **WHEN** RTH 盘中运行 `scan`，而最新一根 K 线的收线时刻早于当前时刻两根 K 线以上
- **THEN** 结果为 `DATA_ERROR`，原因标明新鲜度不足

#### Scenario: 数据恢复
- **WHEN** 缺失之后的下一根 K 线正常到达，且后续校验通过
- **THEN** 该 scope 的数据阻止状态解除，缺口记录仍保留

### Requirement: 快照可追溯
每次校验使用的数据 SHALL 封存为带唯一 ID 和内容摘要的快照，摘要覆盖所选来源、所含 K 线的键与 revision、换月规则版本和日历版本。相同输入 MUST 得到相同摘要。

#### Scenario: 相同输入重复运行
- **WHEN** 对同一 scope、同一 `as_of_utc`、同样的库内容运行两次 `scan`
- **THEN** 两次的快照内容摘要相同

#### Scenario: 修订改变摘要
- **WHEN** 快照范围内的一根 K 线新增了 revision，且该 revision 在 `as_of_utc` 时已可见
- **THEN** 新快照的内容摘要与之前不同
