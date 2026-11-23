# Spec Delta

## ADDED Requirements

### Requirement: 人工轨的表
运行库 SHALL 另含计划审计、纪律事件和 watch 状态的表，并给计划、人工成交、订单事件和纪律提醒补充所需的列。这些结构 MUST 由新的编号 migration 建立，已有数据不受影响。

#### Scenario: 从阶段 3 的库升级
- **WHEN** 对一个已应用前两个 migration 的运行库打开连接
- **THEN** 第三个 migration 被应用，新表存在，原有数据不变
