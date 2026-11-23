## ADDED Requirements

### Requirement: 研究用表
运行库 SHALL 通过编号 migration `0004_research.sql` 新增 `baselines`、`research_runs`、`split_registries`、`data_segments` 四张表，并给 `withheld_coverage` 增加 `content_sha256` 列。既有表的其他结构不变。

#### Scenario: 从阶段 4 的库升级
- **WHEN** 用新代码打开阶段 4 建的运行库
- **THEN** migration 0004 被应用，既有数据保留，全部既有测试通过
