## ADDED Requirements

### Requirement: Memory 版本的 migration
运行库 SHALL 通过编号 migration `0007_memory` 增加 `memory_versions`、`memory_head` 和 `memory_events`，不改动既有表的列。

#### Scenario: 从旧库升级
- **WHEN** 对只应用到 `0006_evolve` 的运行库执行 migrate
- **THEN** 只应用 `0007_memory`，既有数据不变
