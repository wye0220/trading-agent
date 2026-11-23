## ADDED Requirements

### Requirement: memory 子命令
CLI SHALL 提供 `memory`：默认列出头指针的 `memory_version`、生效条目的句子和待处理的候选；`--confirm ID`、`--reject ID`、`--supersede ITEM_ID`（可带 `--evidence`）、`--rollback MEMORY_VERSION`、`--history`、`--context KIND`。输出 JSON 或中文短报。

#### Scenario: 确认偏好候选
- **WHEN** 运行 `memory --confirm <候选标识>`，候选是合法的 `user_preference`
- **THEN** 输出新的 `memory_version` 和生成的句子，退出码 0

#### Scenario: 候选不合法
- **WHEN** 被确认的候选没通过校验
- **THEN** 命令拒绝并给出原因码，退出码 1，不改任何状态
