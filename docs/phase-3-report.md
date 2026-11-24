# 阶段 3 汇报：引擎核心

日期：2026-11-22。分支：`phase-3-engine`（从 `phase-2-strategy-spi` 分出，未合并）。变更：`openspec/changes/archive/2026-11-22-add-engine-core/`。

## 1. 完成项与未完成项

**完成**

- 成交模型（纯函数，版本 `fill-v1.0`）：三种订单类型、成交后保护、三种目标定价、歧义规则、截止强平、原子反手、缺数据不造成交。
- 逐步运行器：推进行情、组装特征、调用策略、风控、挂单、写扫描记录，一个事务提交；幂等；时钟只前进；落下的 K 按顺序补算；提交订单前重读冻结状态。
- 回放函数，带真实数据保护。
- 风控：每手计划风险、按预算算手数、人工计划核验的七个原因码、带事务和版本校验的预留、真实账户跨策略的止损计数、纸面交易的结算与两种 R。
- 版本绑定。
- 给模型的上下文构建器、六个 JSON Schema、输出校验、解释请求队列。
- 运行库 migration `0002_engine`。
- `docs/engine.md`，32 个成交向量。

**未完成**

没有未完成的任务。要说明的四件事：

- **没有新的命令行功能。** Prompt 3 明确规定本阶段不实现 CLI 业务。`scan` 仍然只做数据校验；把策略和引擎接进 `scan` 是阶段 4。
- **仍然没有在真实数据上运行策略。** `replay` 对非合成数据默认拒绝：要显式允许，并且先配置开发可见截止日。真实运行库只是升级了表结构，`engine_runs` 是空的。
- **没有调用模型。** 上下文、schema、队列都是给阶段 4、10 用的。
- **费用和账户限额没有数值。** `config/risk.yaml` 的金额类字段留空。

## 2. 产物路径

- 代码：`src/trading_agent/engine/`（`fills.py`、`runner.py`、`risk.py`、`versions.py`、`llm.py`、`config.py`）
- 存储：`src/trading_agent/migrations/runtime/0002_engine.sql`
- 配置：`config/risk.yaml`、`config/engine.yaml`
- Schema：`schemas/*.schema.json`（6 个）
- 文档：`docs/engine.md`，`docs/glossary.md`、`docs/data-contract.md`、`README.md` 的相应更新
- 测试：`tests/test_fills.py`、`test_risk.py`、`test_engine_runner.py`、`test_llm.py`、`tests/fill_runner.py`、`tests/vectors/fills/*.yaml`

## 3. 执行的命令和真实结果

| 命令 | 结果 |
| --- | --- |
| `uv run pytest` | 431 passed（阶段 1、2 的 284 项加阶段 3 的 147 项） |
| `uv run pytest -m realdata` | 4 passed。真实运行库升级到 `0002_engine`，K 线 4,363,250 根不变 |
| 32 个成交向量 × 正向、镜像 | 全部通过 |

对既有测试的改动只有一处：`tests/test_storage.py` 里断言已应用的 migration 列表，从一个编号改为两个。

合成数据上的端到端结果（`test_pa_replay_end_to_end`）：一个脚本化的交易日里，PA 策略给出 H1、H2 两个信号，各成交一次、各止损一次，第三个信号（H3）被两次止损停手拦截；订单的激活和过期时刻、成交价、止损滑点、标记、版本元组都与手算一致。

## 4. 新增假设及写入位置

`docs/engine.md` 第八部分，共 13 条。最值得你看的：

1. **跨着激活时刻的入场默认成交并打 `AMBIGUOUS_ENTRY` 标记**（假设 1）。提示词包给了"记 MISSED 或 AMBIGUOUS_ENTRY"两种说法。选"不成交"的话，只有 5 分钟线的时段将没有任何成交。
2. **目标和回落限价单不给价格改善**（假设 2）：偏保守。
3. **纸面没有日损上限**（假设 10）：只有策略自己的两次止损停手。

与原计划不同的一处：实施时发现，运行器落后几根 K 再运行时，如果只处理最新一根，策略的状态机会漏掉中间的 K。所以改成按顺序逐根补算，落下的 K 的信号按 `MISSED_LATE_SIGNAL` 处理。这也正是提示词包阶段 10 的要求（"睡眠后补算落下的事件时写历史账本，不能把旧信号当作当前纸面入场"）。

## 5. 进入 `FROZEN_CONTRACT` 的内容

按 `CLAUDE.md`，阶段 1–3 完成并经你验收后，下面这些进入冻结。之后的调整要写编号 migration，并重跑全部既有阶段的测试。**现在还没有冻结，等你验收。**

**表结构**：运行库 `0001_initial` 和 `0002_engine` 建立的全部表；发布库的 `active_pointers`、`release_events`。

**状态枚举**：

| 枚举 | 取值 |
| --- | --- |
| `signal_status` | `WAIT`、`SETUP_VALID`、`DATA_ERROR` |
| `risk_status` | `NOT_EVALUATED`、`PASS`、`BLOCKED` |
| `paper_action` | `NONE`、`QUEUE_ENTRY`、`MANAGE_ONLY`、`EXIT` |
| 订单状态 | `ARMED`、`TRIGGERED`、`FILLED`、`EXPIRED`、`CANCELLED` |
| 订单类型 | `STOP_LIMIT_NEXT_BAR`、`MARKET_NEXT_OPEN`、`LIMIT_RETRACE` |
| 目标描述 | `ABSOLUTE_PRICE_TICKS`、`OFFSET_FROM_FILL_TICKS`、`INITIAL_RISK_MULTIPLE` |
| 退出原因 | `STOP`、`TARGET`、`FORCED_FLAT`、`REVERSAL` |
| 运行模式 | `fixture`、`historical_replay`、`pre_eval_shadow`、`live_shadow`、`paper`、`manual_actual` |

`paper_action` 的 `EXIT` 目前没有被使用：退出由成交模型的事件记录，不作为一步的动作。

**接口**：`StrategyProvider` 及其全部类型（`strategies/base.py`）、`OrderIntent` 协议、`engine.runner.step` 与 `replay`、`engine.fills.apply_bar`、`engine.risk` 的核验与预留函数、`engine.versions.current`、`engine.llm` 的上下文与校验函数。

## 6. 下一阶段可复现的输入

- `runner.create_run(...)`、`runner.step(...)`：阶段 4 的 `scan` 把现在的数据校验和它接起来。
- `risk.check_manual_plan(...)`、`risk.reserve(...)`、`risk.record_account_exit(...)`：阶段 4 的 `plan`、`watch`、人工成交导入。
- `llm.build_scan_context(...)`、`llm.validate_output(...)`、解释请求队列：阶段 4、10 调用 Claude。
- `runner.replay(...)`：阶段 5 的回放与基线。

阶段 4 之前需要你处理的事：

- 验收阶段 1–3，决定是否冻结契约。
- 填 `config/risk.yaml`。
- 阶段 5 之前定下开发可见截止日。
