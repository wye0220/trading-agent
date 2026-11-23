# llm-context-contract Specification

## Purpose

限定模型能看到什么、它的输出必须长什么样，并保证模型出问题时确定性的交易流程不受影响。

## Requirements

### Requirement: 上下文只含裁剪后的结构化事件
给模型的上下文 SHALL 只含结构化的事件和策略给出的决策元数据，MUST NOT 含原始 K 线的完整序列。

#### Scenario: 扫描解释的上下文
- **WHEN** 为一次扫描构建上下文
- **THEN** 其中有信号状态、订单意图的数值和决策元数据，没有逐根的历史 K 线

### Requirement: token 预算
每类调用 SHALL 有 token 预算上限。上下文超出预算时按固定顺序裁掉次要内容；裁到最小仍超出时 MUST 报错，不得静默截断。

#### Scenario: 超出预算
- **WHEN** 证据内容很长，使上下文超出预算
- **THEN** 次要证据被移除，结果标明做过裁剪，核心字段保留

#### Scenario: 无法裁到预算内
- **WHEN** 预算小到连核心字段都放不下
- **THEN** 构建失败并报错

### Requirement: 输出必须通过 schema 校验
系统 SHALL 为 `ScanExplanation`、`PretradeCheck`、`SessionReview`、`Candidate`、`MemoryCandidate`、`ReleaseReview` 各提供一个 JSON Schema。未通过校验的模型输出 MUST 被丢弃并记录，MUST NOT 影响信号、订单、成交或盈亏。

#### Scenario: 合格的输出
- **WHEN** 一份符合 `ScanExplanation` 的输出送去校验
- **THEN** 校验通过

#### Scenario: 多了未定义的字段
- **WHEN** 输出里多出一个 schema 没有定义的字段
- **THEN** 校验不通过，输出被丢弃，记一条告警

#### Scenario: 不影响确定性流程
- **WHEN** 某一步的解释输出校验失败
- **THEN** 这一步的扫描记录、订单和成交与没有解释时完全相同

### Requirement: 解释请求的重试队列
解释请求 SHALL 连同它依据的快照标识进入队列。模型不可用时请求留在队列里重试，重试 MUST 使用原来的快照。超过最大重试次数的请求 SHALL 转入死信并触发告警。

#### Scenario: 模型不可用
- **WHEN** 一个解释请求处理失败
- **THEN** 它留在队列里，尝试次数加一，快照标识不变

#### Scenario: 超过重试上限
- **WHEN** 一个请求连续失败达到上限
- **THEN** 它进入死信队列，产生一条告警，不再重试
