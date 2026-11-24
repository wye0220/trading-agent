# Spec Delta

## ADDED Requirements

### Requirement: 引擎表
运行库 SHALL 另含引擎所需的表：引擎运行记录、纸面仓位、已完结的纸面交易、解释请求队列、账户风险日记录和风险预留。纸面订单 SHALL 记录激活时刻、过期时刻、信号 K 时刻、触发价、限价、止损、目标描述和标记。这些结构 MUST 由一个新的编号 migration 建立，已有数据不受影响。

#### Scenario: 从阶段 1 的库升级
- **WHEN** 对一个只应用过初始 migration、且已有行情数据的运行库打开连接
- **THEN** 新的 migration 被应用，新表存在，原有的 K 线数量不变

#### Scenario: 结构版本
- **WHEN** 升级完成后查询结构版本
- **THEN** 已应用的编号依次为初始 migration 和引擎 migration
