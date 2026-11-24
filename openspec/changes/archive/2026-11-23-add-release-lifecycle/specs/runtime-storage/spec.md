## ADDED Requirements

### Requirement: 发布相关表
运行库 migration `0009_release` SHALL 新增发布版本、版本事件、影子登记和监控记录表；事件表和影子计划 SHALL 不可修改。发布库 migration `0002_release` SHALL 新增已批准清单表，清单和审计事件 SHALL 只追加。

#### Scenario: 改写审计事件
- **WHEN** 试图更新或删除 `release_events`
- **THEN** 数据库拒绝
