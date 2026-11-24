## ADDED Requirements

### Requirement: 运维相关表
运行库 migration `0010_ops` SHALL 新增运维状态（心跳、水位）、运行锁、告警分发记录、控制命令审计、确认令牌和 context 约束表；控制命令审计 SHALL 只追加。

#### Scenario: 改写审计
- **WHEN** 试图更新或删除控制命令审计
- **THEN** 数据库拒绝
