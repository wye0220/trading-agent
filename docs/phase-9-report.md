# 阶段 9 汇报：影子、发布、回滚与发布后监控

分支 `phase-9-release`，OpenSpec 变更 `add-release-lifecycle`（未归档）。没有合并到 `main`。阶段 8 的变更 `add-independent-evaluation` 已归档。

## 1. 完成项与未完成项

完成：

- 五个子命令：`shadow`、`release`、`rollback`、`freeze`、`monitor`。
- 正式影子：只收 `PASS_FOR_SHADOW` 的候选；计划在登记时冻结，到终点才判定，没有延长入口；两臂同一决策时点推进、各写各的账本；运行检查（账本、日终持平、引擎异常、冻结期挂单、数据异常日、最低成交）；可选的非劣效检验（默认关闭）。
- 前向保留窗口：影子结束前，窗口内的交易日不进日复盘和聚合。
- 实时前向预演：队列前三名挂新行情，只记可运行性。
- 发布清单（候选版本和首次纸面基线），首次基线的五项验收。
- 人工批准：批准人名单、交互终端里手输摘要前缀、发布守卫、按当前状态重算清单、发布库一个事务里核验并切换 active、审计只追加、重复批准幂等。
- 盘中扫描在发布库的读快照里解析 active 版本，下一次扫描起切换；旧订单和仓位保留原版本。
- 冻结按原因记录和解除，可撤未成交单；回滚到上一版本，没有上一版本时冻结并停用入场。
- 发布后监控：激活以来的滚动指标，触发即冻结（`MONITOR`）并告警，同日幂等。
- 提示词第 6 节的全部演练（独立的测试数据库和发布库），见 `docs/release-policy.md` 第 6 节。
- `docs/release-policy.md`。

未完成：

- **真实 scope 还没有 active 版本。** 首次纸面基线的清单已生成（`rel_b479bbc4f6dd89ef`），但纸面运行验收没过：常驻纸面运行处理过 0 个交易日，因为截止日之后的行情不进运行库，盘中扫描一直是 `NO_DATA`。
  - 补充（同日，按你的决定）：实时行情从 2026-11-24 起放行进运行库（`config/live_data.yaml`，见 `docs/data-contract.md`）。但采集是每天 17:12 收盘后一次，盘后补进来的 K 线信号都已过期，不会成交；纸面运行验收也只数"当天至少一半的 K 线在收线后 5 分钟内处理过"的交易日，盘后补跑不算。所以盘中成交和验收都要等阶段 10 的盘中采集。
- **即使纸面运行验收过了，现在也批准不了。** 发布守卫要求受保护摘要清单里有发布服务的摘要，清单要由评测／发布身份写入，而隔离（阶段 8）还没做。
- **没有真实的影子。** 阶段 8 没有候选通过正式确认，影子没有可登记的对象。
- 控制通道里的批准和定时调度（每天收盘后跑 `monitor`、盘中跑 `shadow --step`）留给阶段 10。
- 候选链只支持一层（见假设 9）。

## 2. 产物

| 类别 | 路径 |
| --- | --- |
| 代码 | `src/trading_agent/release/`（`config.py`、`guard.py`、`pointer.py`、`manifest.py`、`shadow.py`、`freeze.py`、`monitor.py`、`run.py`）；`scanner.py`（读快照）；`cli.py`；`evolve/aggregate.py`、`review/build.py`（前向保留排除）；`evaluation/guard.py`（摘要清单加 `release_sha256`） |
| 配置 | `config/release.yaml` |
| migration | `migrations/runtime/0009_release.sql`、`migrations/release/0002_release.sql` |
| 测试 | `tests/test_release.py`（25 项）；`test_cli.py`、`test_cli_manual.py`、`test_risk.py`、`test_storage.py` 随新命令和 migration 更新 |
| 文档 | `docs/release-policy.md`、`docs/glossary.md`（阶段 9 一节）、`docs/evolve.md`（排除原因）、`README.md` |
| 规格 | `openspec/changes/add-release-lifecycle/` |

## 3. 命令与真实结果

```text
uv run pytest                      714 passed, 1 skipped, 7 deselected
uv run pytest -m realdata          6 passed
openspec validate --all --strict   39 passed
```

跳过的 1 项是"未实现的子命令不读写数据"：现在所有子命令都已实现，参数集为空。

基线 `bl_6027564e58b04712` 的代码摘要没有变（没有改 `engine/`、`strategies/`）。

真实库上（运行库迁移到 `0009_release` 之前做了备份；发布库只读打开，没有迁移、没有写入）：

| 命令 | 结果 |
| --- | --- |
| `release --status` | 没有 active 版本；常驻纸面运行 `DATA_BLOCKED:NO_DATA`；待批准清单 1 份 |
| `release --initial-baseline` | 清单 `rel_b479bbc4f6dd89ef`；工程 169 项、逐 bar 向量 140 项、账本、只读 4 项通过；纸面运行验收 `NOT_MET`（0／5 个交易日） |
| `shadow --status` | 0 个影子 |
| `freeze --status` | 只有数据冻结（`DATA`），没有人工、监控或发布冻结 |
| `monitor` | 没有 active 版本，没有可监控的对象 |
| `evaluate --digests` | 新增发布服务摘要；清单状态仍是 `AUTHOR_IDENTITY` |

首次基线清单的回滚触发条件（单位 R，来自阶段 5 开发段）：滚动 20 日日均净值低于 −0.41；激活以来回撤超过 22.28；成交频率超出 [0.10, 0.90]／日；信号频率超出 [0.17, 1.49]／日；`AMBIGUOUS_BAR` 占比超过 5%。

清单里的 `git_commit` 是准备时的提交，工作区有未提交改动时会标出来；提交后重新准备，`manifest_hash` 会变。

## 4. 假设

12 条，写在 `docs/release-policy.md` 末尾。最需要你确认的：

1. 人工确认靠交互终端加手输摘要前缀，挡误操作，不是安全边界。
2. 发布服务代码和评测器共用一份受保护摘要清单；清单写入前，真实 scope 不能发布。
3. 影子计划：20 至 60 个有效日、每臂 15 笔。ES 按开发段频次会排满 60 日，预期成交打八折后不够 15 笔，结果可能是 `EVIDENCE_INSUFFICIENT`。
4. 冻结作用域：`MANUAL` 停整个 scope（含影子），`MONITOR` 和 `RELEASE` 只停常驻纸面运行。
5. 监控阈值：3σ、1 倍开发段最大回撤、频率 3 倍、`AMBIGUOUS_BAR` 3 倍或 5%。
6. 候选链只支持一层：一旦某个候选版本成为 active，之后的候选批准时会是 `STALE_BASE`，直到评测支持以发布版本为基线。

## 5. 下一阶段的输入

阶段 10（调度、告警与控制通道）：

- 盘中每 5 分钟：`scan`（已在读快照里解析 active）、`shadow --step`。
- 每个交易日收盘后：`monitor`；触发时它已经冻结并调用 `ops/notify-telegram.sh`。
- 控制通道：经身份校验的批准、回滚、解冻，替代终端确认（`Approval(channel="CONTROL_CHANNEL")` 已预留）。
- 让真实的首次纸面基线能批准，需要两件事：截止日之后的实时行情能以某种身份进运行库，常驻纸面运行跑满 5 个交易日；隔离设置完成，评测／发布身份写入摘要清单。
