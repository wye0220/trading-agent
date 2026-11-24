## Context

告警写入 `alerts` 表已遍布各处，但没有分发；模型请求队列和死信转入已有，没有消费者；watch 有心跳但没人看；`scan`、`shadow --step`、`review`、`monitor`、`evolve` 都要人手动跑。`engine/`、`strategies/` 在基线代码摘要里，不能改。

## Decisions

1. **一个 `ops` 子命令、几个动作**：节拍、盘后、周度、分发、模型、死信、健康。launchd 只调用这些命令；手工运行完全等价。
2. **运行锁**放在运行库（`ops_locks`，带有效期），过期的锁可被接管。水位和心跳放在 `ops_state`。
3. **去重**：节拍对每个 scope 记"已处理的最后一根 5 分钟线收线时刻"，没有更新的已可用线就跳过。补跑由引擎保证：落下的信号只记录、不挂单。
4. **告警**沿用 `alerts` 表；分发结果另记在 `alert_deliveries`。去重键 = 级别 + 类型 + scope + 规则细节（如持仓键）；静默窗口按类型配置。纪律提醒在 watch 每轮之后即时分发。
5. **通知适配器**：`telegram`（`ops/notify-telegram.sh`）、`none`；测试用内存适配器。
6. **控制通道适配器**：本机命令行（身份 = 系统用户，需在 `config/control.yaml` 绑定）和 Telegram（专用机器人 token 放在 `<data_root>/secrets/control-telegram.env`，绑定用户 ID；没配置就不启用）。不复用 trading-desk 的机器人，避免两边抢消息。
7. **审批经控制通道**：`Approval(channel="CONTROL_CHANNEL", actor="control:<用户>")`，发送者须在 `config/control.yaml` 标为批准人；发布守卫照常核对。
8. **context**：`no-entry until HH:MM [scope|ALL]` 记约束并以冻结原因 `CONTEXT` 冻结纸面入场，到期由政策自动解除；`max-qty N` 只能不大于已批准上限。人工计划预检读取生效约束。
9. **模型调用**：`claude -p --tools ""`（没有任何工具），提示词里只放封存的上下文；输出解析后过 schema；失败沿用 `fail_explanation` 的重试和死信。
10. **watch 间隔**：控制通道写 `ops_state` 的覆盖值，watch 循环每轮重读；命令行 `--interval` 优先于覆盖值，覆盖值优先于 Memory 偏好和配置。

## Risks / Trade-offs

- 盘中行情现在没有：节拍在真实环境里只有收盘后才会看到新线，盘中扫描要等盘中采集脚本被你授权安装。
- 每 5 分钟起一个 `claude -p` 会话采集，成本和偶发失败需要权衡；失败时节拍记 `DATA_STALE` 告警，不会产生错误信号。
- 本机命令行通道和批准人是同一个系统用户；它只防误操作，不是安全边界。
