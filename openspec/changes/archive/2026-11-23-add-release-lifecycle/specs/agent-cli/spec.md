## ADDED Requirements

### Requirement: 发布相关子命令
CLI SHALL 提供 `shadow`（`--register`、`--step`、`--finish`、`--status`）、`release`（`--prepare`、`--initial-baseline`、`--approve`、`--manifest-hash`、`--status`）、`rollback`、`freeze`（`--cause`、`--reason`、`--cancel-working`、`--unfreeze`、`--status`）和 `monitor`。输出 JSON 或中文短报。

#### Scenario: 查看发布状态
- **WHEN** 运行 `release --status`
- **THEN** 列出各 scope 的 active 版本、入场权限、冻结原因和待批准的清单
