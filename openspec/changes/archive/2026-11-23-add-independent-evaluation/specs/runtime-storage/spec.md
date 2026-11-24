## ADDED Requirements

### Requirement: 评测的 migration
运行库 SHALL 通过 `0008_evaluation` 增加协议、登记、窗口账本和事故库的表，不改既有表的列。

#### Scenario: 从旧库升级
- **WHEN** 对应用到 `0007_memory` 的库执行 migrate
- **THEN** 只应用 `0008_evaluation`
