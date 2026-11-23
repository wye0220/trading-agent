## ADDED Requirements

### Requirement: 复盘用表
运行库 SHALL 通过编号 migration `0005_review.sql` 新增 `session_reviews`、`observations`、`incident_drafts`、`memory_candidates` 四张表。既有表结构不变。

#### Scenario: 升级
- **WHEN** 用新代码打开阶段 5 的运行库
- **THEN** migration 0005 被应用，既有数据保留
