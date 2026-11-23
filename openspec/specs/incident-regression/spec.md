# incident-regression Specification

## Purpose

维护经确认、能独立重放的事故回归库，以及评测前必须通过的固定合成回归。

## Requirements

### Requirement: 事故回归库
回归库 SHALL 只收录经用户确认的事故。条目 SHALL 含草稿的检查点、从检查点到事故处理后的原始事件、事故日及预热期的原始行情和交易日状态、以及期望结果的摘要。收录时 SHALL 独立重放一次；重放不出来的 SHALL 标 `INCOMPLETE`，不算覆盖。

#### Scenario: 可重放
- **WHEN** 确认一份草稿，临时库里重放得到与记录一致的决策序列、订单和交易
- **THEN** 条目状态 `CONFIRMED`

#### Scenario: 重放不出来
- **WHEN** 重放结果与记录不一致，或缺检查点、缺数据
- **THEN** 条目状态 `INCOMPLETE`，回归统计里单列

### Requirement: 评测前先跑回归
每次评测 SHALL 先重放全部 `CONFIRMED` 条目并运行合成回归。任一失败 SHALL 使评测 `FAIL`。

#### Scenario: 行为变了
- **WHEN** 某个条目的重放结果不再与期望一致
- **THEN** 回归失败，列出该条目
