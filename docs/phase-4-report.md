# 阶段 4 汇报：scan、plan、watch 与人工成交导入

日期：2026-11-22。分支：`phase-4-scan-plan-watch`（从 `phase-3-engine` 分出，未合并）。变更：`openspec/changes/add-scan-plan-watch/`。

## 1. 完成项与未完成项

**完成**

- `scan` 接入策略和引擎：盘中用常驻纸面运行，历史用一次性运行回放当日；中文短报；资讯窗口配置；真实数据保护。
- `plan`：一行快捷语法、预检的三部分与合成结论、无新形态重试的标记、激活时预留、取消与过期释放、状态审计。
- 人工成交与订单事件导入：幂等、隔离、开平判定（含跨零拆分）、先进先出盈亏、账户止损计数、计划关联的四种情形、分批成交。
- 纪律事件七类，`plan_adherence` 与 `strategy_alignment` 分开；交易员纠正；每日 KPI。
- `watch`：只读适配器接口、本地快照文件适配器、六条规则、过期不推断、静默窗口、条件解除、心跳、可调间隔。
- 运行库 migration `0003_manual`，`config/manual.yaml`、`config/news_windows.yaml`，`docs/scan-plan-watch.md`（含五段由测试生成并逐字比对的样例）。

**未完成**

没有未完成的任务。但现在这些功能能做的有限，原因都在数据上：

- **没有实时行情。** 盘中 `scan` 在真实环境里现在只会报 `DATA_ERROR`（没有数据）。
- **没有真实券商接口。** `watch` 只能读本地快照文件；没有文件时每轮都是"读取失败"告警。
- **历史 `scan` 在真实数据上不运行策略**，因为没有设开发可见截止日。它只给数据校验结果并说明原因。
- **账户限额没有填。** 每份计划的预检结论现在都是 `BLOCKED_BY_RISK`（`BUDGET_NOT_APPROVED`）。
- 控制通道推送、常驻调度、用 Claude 写解释是阶段 10。提醒现在写进数据库并打印。

## 2. 产物路径

- 代码：`src/trading_agent/scanner.py`，`src/trading_agent/manual/`（`plans.py`、`trades.py`、`discipline.py`、`watch.py`、`broker.py`、`common.py`、`config.py`），`cli.py` 的扩展
- 存储：`src/trading_agent/migrations/runtime/0003_manual.sql`
- 配置：`config/manual.yaml`、`config/news_windows.yaml`
- 文档：`docs/scan-plan-watch.md`，以及 `docs/glossary.md`、`docs/data-contract.md`、`README.md` 的更新
- 测试：`tests/test_plans.py`、`test_trades.py`、`test_watch.py`、`test_scanner.py`、`test_cli_manual.py`、`tests/manual_helpers.py`

## 3. 执行的命令和真实结果

| 命令 | 结果 |
| --- | --- |
| `uv run pytest` | 538 passed |
| `uv run pytest -m realdata` | 4 passed；真实运行库升级到 `0003_manual` |
| `scan --as-of 2026-09-03T15:00:00Z`（真实数据） | `WAIT`，"策略没有运行：这是真实数据，而开发可见截止日没有设置……" |
| `scan`（真实环境，盘中） | `DATA_ERROR`：`NO_DATA`，没有新订单 |
| `watch --once --account demo` | `READ_FAILED`：没有账户快照文件 |

对既有测试的改动：

- `tests/test_cli.py`：未实现的子命令从 11 个变为 9 个；`scan` 的测试改为核对新的输出（策略已接入）。
- `tests/test_storage.py`、`tests/test_risk.py`：已应用的 migration 列表多了 `0003_manual`。

真实运行库里多了一条空的常驻纸面运行记录（`paper:pa_brooks:ES:RTH:5m`），是我验证盘中 `scan` 时建的，没有订单。

## 4. 新增假设及写入位置

`docs/scan-plan-watch.md` 第八部分，共 12 条。最值得你看的：

1. **无法确认与策略一致时，预检结论给 `DEVIATES`**（假设 1）。`strategy_alignment` 单独如实报告 `UNKNOWN`。
2. **先导入订单事件，再导入成交**。反过来的话，已登记的止损次数不会回头修改。
3. **`watch` 靠前后两次快照比较**（假设 10）：两次采集之间开了又平的仓位看不到；首次运行时已有的持仓会被当作新出现的。
4. **只有成交、没有订单事件时**，提前离场、放宽止损、延迟止损一概不判，记 `MISSING_EVIDENCE`。这是提示词包的要求。

与原计划不同的一处：`scan` 的数据校验原来自己写一条扫描记录，接入引擎后会和引擎的记录重复，所以盘中和历史 `scan` 里校验这一步不再写（直接调用校验函数时仍会写）。

## 5. 契约

阶段 3 汇报里列出的"待冻结"内容还没有经你确认。本阶段新增了一个编号 migration（`0003_manual`），没有改动已有的状态枚举和引擎接口；`scan.run_scan` 增加了两个可选参数。

## 6. 下一阶段可复现的输入

- `scanner.live_scan`、`scanner.historical_scan`
- `manual.plans`、`manual.trades`、`manual.discipline`、`manual.watch`
- `engine.runner.replay`：阶段 5 的回放与基线直接在它上面做。

阶段 5（回放与基线）**必须先有开发可见截止日**，否则回放函数拒绝在真实数据上运行。
