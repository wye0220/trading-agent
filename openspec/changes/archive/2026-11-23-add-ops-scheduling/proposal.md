## Why

阶段 1–9 的每一步都要人手动运行：没有盘中节拍、盘后流水、告警分发，也没有经身份校验的控制通道。阶段 10 把它们串成可调度的流水，并给出恢复手段。调度只生成样例和等价的手工命令，你授权后才安装。

## What Changes

- 新增 `ops/` 模块和 `ops`、`control` 两个子命令。
- 盘中节拍（`ops --tick`）：运行锁、数据水位去重，只在新的 5 分钟线收线且可用后对每个已登记 scope 扫描一次，并推进影子；持久化水位和心跳；行情断流、迟到告警。
- 盘后（`ops --daily`）：确认当日 K 线到齐 → 封存并复盘（策略轨与人工轨）→ 账本检查 → 发布后监控；提前收盘随日历；前向保留窗口里的复盘跳过，只记运行健康。
- 每周（`ops --weekly`）：每周最后一个交易日按 scope 运行聚合。
- 告警分发（`ops --dispatch`）：按级别、去重键和静默窗口推送；纪律提醒即时推送；watch 心跳停止或过期时 CRITICAL。
- 模型调用（`ops --explain`）：`claude -p` 无工具调用，只喂封存的上下文，输出过 JSON schema；连续失败进死信队列并告警；`ops --dlq-replay` 重放。
- 控制通道（`control`）：命令白名单、来源与绑定用户校验、全部写审计；`approve` 回显 manifest_hash；`unfreeze` 需要 scope、freeze_event_id 和短期确认令牌；`context` 只能收紧风险；`watch-interval` 动态改 watch 轮询间隔。适配器：本机命令行和 Telegram（独立机器人，配置后才启用）。
- `ops --health`：心跳、水位、锁、死信、待发告警和恢复命令。
- launchd 样例（`ops/launchd/*.example`）和 `docs/ops-runbook.md`（等价手工命令、恢复、全链路演示）。
- 新增 `config/ops.yaml`、`config/control.yaml`、migration `0010_ops`。

## Capabilities

### New Capabilities
- `ops-scheduling`: 盘中节拍、盘后与周度流水、水位与心跳、运行锁。
- `ops-alerts`: 告警级别、去重与静默、通知适配器、watch 停止告警。
- `control-channel`: 命令白名单、身份校验、确认令牌、审计、context 约束。
- `ops-recovery`: 模型调用与死信队列、健康检查与恢复命令。

### Modified Capabilities
- `agent-cli`: `ops`、`control` 子命令。
- `runtime-storage`: migration `0010_ops`。

## Impact

- 不改 `engine/`、`strategies/`；基线不失效。
- 不安装任何定时任务；样例和命令在文档里，你授权后再装。
- 盘中节拍要有盘中行情。现在每日采集只在收盘后取一次；盘中采集脚本样例沿用 `claude -p` 调 Webull 连接器，每 5 分钟一次，开销和稳定性要你权衡。
- Telegram 控制通道要一个专用机器人和你的用户 ID；没有配置时只有本机命令行通道。
