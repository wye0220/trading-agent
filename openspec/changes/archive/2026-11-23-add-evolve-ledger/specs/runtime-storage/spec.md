## ADDED Requirements

### Requirement: 演化相关的表
运行库 SHALL 通过编号 migration `0006_evolve.sql` 新增 `evolve_runs`、`observation_clusters`、`candidate_queue` 和 `pre_eval_shadow_days`。既有表 MUST NOT 被改动。

#### Scenario: 升级已有的库
- **WHEN** 在已有数据的运行库上执行 migration
- **THEN** 新表出现，既有数据不变
