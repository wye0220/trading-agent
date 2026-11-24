# ops-recovery Specification

## Purpose

规定模型调用不给工具、输出须过 JSON schema、连续失败转入死信队列并可重放，以及健康检查报告心跳、水位、运行锁、死信和各类故障的恢复命令。

## Requirements

### Requirement: 模型调用与死信队列
`ops --explain` SHALL 用 `claude -p` 处理待解释请求，不给模型任何工具，只喂封存的上下文；输出 SHALL 通过对应 JSON schema。连续失败达到上限 SHALL 转入死信队列并记 WARN；确定性流程 SHALL 不受影响。`ops --dlq-replay` SHALL 把死信重新放回队列。

#### Scenario: 输出不合 schema
- **WHEN** 模型三次输出都不符合 schema
- **THEN** 请求转入死信队列，记 WARN

### Requirement: 健康检查与恢复命令
`ops --health` SHALL 报告各心跳和水位、运行锁、死信数、未分发告警，并列出失去行情、迟到、停机、重复运行、控制通道离线、券商接口失效、死信重放的恢复命令。

#### Scenario: 过期的锁
- **WHEN** 运行锁超过有效期仍未释放
- **THEN** 健康检查标出它，下一次节拍可以接管
