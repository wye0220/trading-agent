# Spec Delta

## Purpose

由已收线的原始行情派生更高周期的 K 线和参考价位，并记录每个结果何时可知、依赖哪些数据，保证回放时看不到未来。

## ADDED Requirements

### Requirement: 5 分钟线派生
来源只提供 1 分钟线时，系统 SHALL 由同一实际合约、同一交易时段的 1 分钟线聚合出按纽约时间整 5 分钟对齐的 5 分钟线，并记录参与聚合的 1 分钟线数量。区间内没有任何 1 分钟线时 MUST NOT 生成该 5 分钟线。来源直接提供 5 分钟线时，系统 SHALL 原样使用。

#### Scenario: 来源直接提供 5 分钟线
- **WHEN** 某交易日所选来源是 Webull 采集的 5 分钟线
- **THEN** 该交易日的 5 分钟线与采集到的内容一致，不做聚合

#### Scenario: 完整的 5 分钟
- **WHEN** 09:30–09:34 ET 的 5 根 1 分钟线都存在
- **THEN** 生成 09:30 的 5 分钟线，开盘取第一根的开盘、收盘取最后一根的收盘、高低取极值、成交量求和，构成数量为 5

#### Scenario: 部分分钟无成交
- **WHEN** 某 5 分钟区间内只有 3 根 1 分钟线
- **THEN** 生成该 5 分钟线，构成数量为 3

#### Scenario: 整段无成交
- **WHEN** 某 5 分钟区间内没有 1 分钟线
- **THEN** 不生成该 5 分钟线

#### Scenario: 不跨时段
- **WHEN** 聚合 09:25–09:35 ET 附近的数据
- **THEN** 09:25 的线归 ETH，09:30 的线归 RTH，两者互不包含对方的 1 分钟线

### Requirement: 已完结 1 小时线
系统 SHALL 由同一实际合约的数据构建 1 小时线，且只在该小时结束且数据可用后才视为已完结。小时内存在非预期缺口时 MUST NOT 生成完整的 1 小时线。

#### Scenario: 小时未结束
- **WHEN** 在 10:40 ET 查询已完结的 1 小时线
- **THEN** 最新一根是 09:00–10:00 那一根，10:00 开始的那一根不可见

#### Scenario: 小时内缺 bar
- **WHEN** 某小时内有一个非预期缺口
- **THEN** 该小时不生成 1 小时线，并记录原因

### Requirement: RTH 日高低差
系统 SHALL 为每个完整的 RTH 交易日记录日内高低差，供之后计算 ADR。有非预期缺口的 RTH 日 MUST NOT 被记为完整有效日。当前未结束的交易日 MUST NOT 出现在供 ADR 使用的结果中。

#### Scenario: 当天不计入
- **WHEN** 在某交易日盘中查询供 ADR 使用的日高低差
- **THEN** 结果只包含此前已收盘的 RTH 交易日

#### Scenario: 有缺口的交易日
- **WHEN** 某 RTH 日存在非预期缺口
- **THEN** 该日不计为完整有效日，查询结果标明原因

### Requirement: 参考价位
系统 SHALL 为每个交易日提供 PDH、PDL、ONH、ONL 和上一 RTH 收盘。这些价位 MUST 取自同一实际合约；换月使前一日数据属于另一合约时，对应价位标为不可用。

#### Scenario: 普通交易日
- **WHEN** 在某 RTH 开盘时查询参考价位
- **THEN** PDH、PDL 和上一 RTH 收盘来自前一 RTH 交易日，ONH、ONL 来自当日 RTH 开盘前的 ETH 时段

#### Scenario: 换月当日
- **WHEN** 当日实际合约与前一交易日不同，且新合约在前一日没有完整 RTH 数据
- **THEN** PDH、PDL 和上一 RTH 收盘标为不可用

### Requirement: 因果可见性
每个派生结果 SHALL 记录它依赖的 K 线、各自的 revision 和 `known_at_utc`，其中 `known_at_utc` 取全部依赖中最晚的 `available_at_utc`。按决策时点查询时，系统 MUST 只返回 `known_at_utc` 不晚于该时点的结果，并使用当时已可见的 revision。

#### Scenario: 决策时点早于可用时点
- **WHEN** 一根 5 分钟线的 `known_at_utc` 为 09:35:05，在 09:35:02 查询
- **THEN** 该 5 分钟线不在结果中

#### Scenario: 迟到的修订
- **WHEN** 某根 K 线在 T2 时刻才出现新的 revision，而查询时点为更早的 T1
- **THEN** 查询结果使用 T1 时已可见的旧 revision

### Requirement: 派生结果可重现
对相同的原始数据和配置，派生结果 SHALL 完全相同。

#### Scenario: 重建派生数据
- **WHEN** 清空派生结果后重新生成
- **THEN** 新结果与清空前逐条一致
