# causal-features Specification

## Purpose

从决策时点已可见的 K 线计算策略共用的特征（EMA、ATR、swing、区间、ADR、1 小时线），并明确每个特征何时可用、不可用时如何表示，保证策略在回放和盘中都看不到未来的数据。

## Requirements

### Requirement: EMA20
系统 SHALL 用收盘价计算 20 周期指数移动平均。已有 K 线不足 20 根时 MUST 报告为不可用，而不是给出一个数值。

#### Scenario: 数据不足
- **WHEN** 只有 19 根 K 线
- **THEN** EMA20 为不可用

#### Scenario: 数据足够
- **WHEN** 有 20 根收盘价相同的 K 线
- **THEN** EMA20 等于该收盘价

### Requirement: ATR14
系统 SHALL 用 Wilder 平滑计算 14 周期平均真实波幅。真实波幅不足 14 个时 MUST 报告为不可用。计算结果 MUST NOT 被取整到整数 tick 后再与阈值比较；它以不粗于百万分之一 tick 的固定精度保存。

#### Scenario: 首个可用值
- **WHEN** 恰好有 14 个真实波幅
- **THEN** ATR14 等于它们的算术平均（在固定精度内）

#### Scenario: 之后的平滑
- **WHEN** 已有 ATR 值 A，新的真实波幅为 T
- **THEN** 新的 ATR 等于 (13×A + T) ÷ 14（在固定精度内）

### Requirement: 指标不跨合约接续
EMA 和 ATR SHALL 只用同一实际合约的 K 线计算。换月后 MUST 从新合约的数据重新起算。

#### Scenario: 换月之后
- **WHEN** 当前实际合约只有 10 根 K 线，之前的 K 线属于另一个合约
- **THEN** EMA20 和 ATR14 都不可用

### Requirement: swing 高低点
系统 SHALL 以左右各 2 根严格确认 swing：高点必须严格高于左右各 2 根的高点，低点镜像。等高或等低 MUST NOT 确认。一个 swing 只有在它右侧第 2 根 K 线可见之后才可用。

#### Scenario: 右侧只有一根
- **WHEN** 候选高点右侧只有 1 根已收线的 K
- **THEN** 该 swing 不可用

#### Scenario: 等高不确认
- **WHEN** 候选高点与它右侧第 1 根的最高价相等
- **THEN** 不确认为 swing 高点

#### Scenario: 严格更高
- **WHEN** 一根 K 的最高价严格高于左右各 2 根
- **THEN** 在右侧第 2 根收线后确认为 swing 高点

### Requirement: 区间特征
系统 SHALL 提供最近 20 根已完成 5 分钟线的高低区间，以及当日 RTH 截至当前 K 线的高低区间。两者是不同的特征，MUST 分别标识。不足 20 根时滚动区间不可用。

#### Scenario: 两个区间各算各的
- **WHEN** 当日 RTH 只走了 8 根，而此前的夜盘有更高的高点
- **THEN** 滚动 20 根区间包含夜盘的高点，当日 RTH 区间不包含

### Requirement: ADR
系统 SHALL 以当前交易日之前 10 个 RTH 交易日中完整有效日的高低差均值作为 ADR，并报告有效日数。有效日少于 3 个时 MUST 报告为不可用。当前交易日 MUST NOT 计入。

#### Scenario: 有效日不足
- **WHEN** 之前 10 个交易日里只有 2 个完整有效日
- **THEN** ADR 不可用

#### Scenario: 部分有效
- **WHEN** 之前 10 个交易日里有 7 个完整有效日
- **THEN** ADR 是这 7 天高低差的均值，有效日数为 7

### Requirement: 1 小时线背景
系统 SHALL 提供最近 10 根在决策时点已完结且可见的 1 小时线。不足 10 根时如实给出实有的根数。

#### Scenario: 当前小时未结束
- **WHEN** 决策时点为 10:40 ET
- **THEN** 提供的 1 小时线中最新一根结束于 10:00 ET

### Requirement: 特征只用已可见的数据
任何特征 SHALL 只由决策时点已收线且已可见的 K 线计算。之后才出现的 K 线或修订 MUST NOT 影响该时点的特征值。

#### Scenario: 追加未来的 K 线
- **WHEN** 在一串 K 线后面再追加若干根，重新计算较早时点的特征
- **THEN** 较早时点的特征值不变
