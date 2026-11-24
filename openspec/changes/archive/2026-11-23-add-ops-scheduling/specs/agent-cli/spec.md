## ADDED Requirements

### Requirement: ops 与 control 子命令
CLI SHALL 提供 `ops`（`--tick`、`--daily`、`--weekly`、`--dispatch`、`--explain`、`--dlq-replay`、`--health`）和 `control`（`--exec` 本机命令、`--poll` 拉取适配器消息）。输出 JSON 或中文短报。

#### Scenario: 健康检查
- **WHEN** 运行 `ops --health`
- **THEN** 列出心跳、水位、锁、死信和恢复命令
