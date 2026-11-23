## ADDED Requirements

### Requirement: evaluate 子命令
CLI SHALL 提供 `evaluate`：`--register`、`--screen`、`--confirm`、`--labels`、`--status`、`--regression`、`--incident-list`、`--incident-confirm`、`--isolation-check`、`--digests`、`--accept-digests`。输出 JSON 或中文短报。

#### Scenario: 查看状态
- **WHEN** 运行 `evaluate --status`
- **THEN** 列出登记、各阶段状态、窗口占用和余下的 α
