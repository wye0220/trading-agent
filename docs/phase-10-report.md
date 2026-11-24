# 阶段 10 汇报：调度、告警、控制通道与恢复

分支 `phase-10-ops`，OpenSpec 变更 `add-ops-scheduling`（未归档）。没有合并到 `main`。阶段 9 的变更 `add-release-lifecycle` 已归档。

## 1. 完成项与未完成项

完成：

- `ops` 子命令：盘中节拍（运行锁、水位去重、断流与迟到告警、补跑不恢复过期挂单）、盘后流水（线到齐 → 封存复盘 → 账本检查 → 发布后监控，前向保留窗口只记健康）、周度聚合、告警分发、watch 停止检查、模型调用、死信列出与重放、健康检查。
- 告警分发：级别、去重键、静默窗口、通知适配器；纪律提醒在 watch 每轮之后即时推送；第一次分发时积压的告警不推送。
- 模型调用：`claude -p --tools ""`，只喂封存的上下文，输出过 JSON schema；连续 3 次失败转入死信并告警。
- `control` 子命令和控制通道：命令白名单、来源与绑定用户校验、全部写只追加的审计；`approve` 回显完整 manifest_hash；`unfreeze` 两步确认，令牌绑定原请求、通道和发送者；`context` 只能收紧（禁止入场冻结纸面并挡人工计划、手数上限）；`watch-interval` 下一轮起生效。适配器：本机命令行、Telegram（专用机器人）。
- launchd 样例 7 个和包装脚本 5 个（`ops/launchd/`），没有安装。
- 两期全链路演示（临时库、合成行情、合成券商快照、测试发布指针）。
- `docs/ops-runbook.md`：一天的流水、等价手工命令、告警、控制通道、恢复、演示、假设。

未完成：

- **没有安装任何新的定时任务。** 要你授权。
- **控制通道两个适配器都还不能执行命令**：本机命令行的绑定用户是空的（真实运行时 `freeze ALL` 被拒绝，`UNAUTHORIZED`），Telegram 没有启用、没有专用机器人。
- **盘中行情还没有。** 盘中采集脚本是样例；不装它，节拍在真实环境里要到收盘后才看得到当天的线。
- 运行库里有 1362 个待处理的解释请求（1348 个日复盘叙述、14 个候选归纳），都是之前阶段入队的；`ops --explain` 每次处理 10 个、每个起一个 `claude -p` 会话，我没有在真实库上运行它。

## 2. 产物

| 类别 | 路径 |
| --- | --- |
| 代码 | `src/trading_agent/ops/`（`config.py`、`state.py`、`tick.py`、`daily.py`、`alerts.py`、`explain.py`、`control.py`、`adapters.py`、`context.py`、`health.py`）；`cli.py`；`manual/watch.py`（每轮重读间隔）、`manual/plans.py`（预检读 context）、`release/freeze.py`（`CONTEXT` 冻结政策）、`release/guard.py`（控制通道批准） |
| 配置 | `config/ops.yaml`、`config/control.yaml`；`config/release.yaml` 的冻结政策加 `CONTEXT` |
| migration | `migrations/runtime/0010_ops.sql` |
| 调度样例 | `ops/launchd/com.wenye.trading-agent-{intraday,watch,watch-check,daily,weekly,explain,control}.plist.example`；`run-ops.sh`、`run-intraday.sh`、`run-watch.sh`、`run-control.sh` |
| 测试 | `tests/test_ops.py`（19 项）；`test_cli.py`、`test_cli_manual.py`、`test_risk.py`、`test_storage.py` 随新命令和 migration 更新 |
| 文档 | `docs/ops-runbook.md`、`docs/glossary.md`（阶段 10 一节）、`README.md` |
| 规格 | `openspec/changes/add-ops-scheduling/` |

## 3. 命令与真实结果

```text
uv run pytest                      738 passed, 1 skipped, 7 deselected
uv run pytest -m realdata          6 passed
openspec validate --all --strict   43 passed
```

基线 `bl_6027564e58b04712` 的代码摘要没有变。

真实库上（迁移到 `0010_ops` 之前做了备份）：

| 命令 | 结果 |
| --- | --- |
| `ops --health` | 没有心跳（调度没装）、没有水位和锁；死信 0，待解释 1362，待分发告警 29 |
| `ops --tick` | ES `NO_NEW_BAR`（今天的线已封存，明天起放行）；首次分发把 29 条历史告警标为 `BACKLOG`，没有推送 |
| `control --exec "freeze ALL 演练"` | `REFUSED（UNAUTHORIZED）`，写了一条审计 |
| `ops --weekly` | `NOT_LAST_TRADING_DAY` |
| `ops --watch-check` | `OUT_OF_SESSION`（20:13 ET） |
| `control --poll` | Telegram 没有启用 |
| `ops --dlq` | 死信队列是空的 |

## 4. 假设

10 条，写在 `docs/ops-runbook.md` 末尾。最需要你确认的：

1. 盘中采集沿用 `claude -p` 调 Webull 连接器，每 5 分钟一次。
2. Telegram 控制通道用专用机器人，不复用 trading-desk 的。
3. 告警静默窗口（缺省 30 分钟，断流 15 分钟，watch 停止 10 分钟，裸仓 5 分钟）。
4. `approve` 只要求回显完整摘要，不再要令牌；`unfreeze` 的令牌 120 秒有效。

## 5. 下一步的输入

- 授权安装哪些样例（见 `docs/ops-runbook.md` 第 1 节的表）。
- 本机命令行通道绑定谁、是否批准人：`config/control.yaml` 的 `adapters.cli.users`。
- Telegram 控制通道：专用机器人 token、聊天 ID、你的用户 ID。
- 积压的 1362 个解释请求要不要处理（`ops --explain` 的开销）。
