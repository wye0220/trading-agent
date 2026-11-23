# Proposal

## Why

阶段 1 已经能把 ES 的历史行情按因果规则读出来，但系统还不会判断任何信号。提示词包的 Prompt 2 是下一步：先定下所有策略共用的插件接口，再在它上面实现首版 Al Brooks 价格行为策略 PA v1.0。阶段 3 的引擎、阶段 4 的扫描器和阶段 5 的回放都要调用这个接口，所以它必须先定。

## What Changes

- 新增 **`StrategyProvider` 插件接口**：策略声明需要的特征，接收只读的特征上下文和上一轮状态，返回信号状态、标准 `OrderIntent`、决策元数据和可序列化的下一轮状态。另有 `evaluate_pretrade_alignment`，供阶段 4 判断人工计划是否符合策略。新增策略注册表。
- 新增 **通用因果特征**的纯函数：EMA20、ATR14（Wilder）、左右各 2 根的 swing、最近 20 根滚动区间、当日 RTH 区间、ADR（前 10 个 RTH 日，至少 3 个有效日）、最近 10 根已完结 1 小时线。全部只用决策时点已可见的数据。
- 新增 **PA v1.0 策略插件** `src/trading_agent/strategies/pa_brooks.py`（纯 Python，无 I/O）：
  - **Spike 回调是唯一会生成订单意图的形态**：尖峰识别、回调状态机、H1–H3 / L1–L3 计数、回撤与根数失效、趋势恢复结束。
  - 信号 K 质量检查、过滤器链及豁免表、尾盘 30 分钟不追单、两次止损停手。
  - 订单意图：下一根有效的止损限价单、保护止损、ADR 或 2R 目标。
  - 其余九类形态（高潮反转、MTR、楔形、失败突破、双顶双底、更低高点／更高低点、旗形、H2／L2、H1／L1）**只登记优先级和启用开关，全部关闭**。提示词包规定它们的阈值要先出量化草案、经你确认后才能生成订单。
- 新增策略配置 `config/strategies/pa-esnq-5m-v1.0.yaml`：ES／MES 与 NQ／MNQ 两组参数，各形态的启用状态。
- 新增文档 `docs/strategy-spec.md`：接口规范、PA v1.0 规格、**非 Spike 形态与 1 小时趋势的量化草案**（供你确认）、假设。
- 新增逐 bar 测试向量 `tests/vectors/strategy/pa_brooks/*.yaml`，多空镜像。

不在本次范围：

- 不接入 `scan`、不写纸面订单、不模拟成交（阶段 3、4）。插件只是一个被测试调用的纯函数。
- **不在真实历史数据上运行策略。** 目前没有设开发可见截止日（`HOLDOUT_BOUNDARY_NOT_SET`），在全部历史上看策略结果会污染以后的盲测数据。本阶段只用手写的测试向量验收。
- 不实现 `MARKET_NEXT_OPEN`、`LIMIT_RETRACE` 的成交逻辑（阶段 3）；接口里只定义这些订单类型的字段。

## Capabilities

### New Capabilities

- `strategy-plugin-spi`: 所有策略共用的插件接口、标准订单意图与目标描述、策略状态的序列化、策略注册表。
- `causal-features`: 供策略使用的通用特征及其可见性规则（EMA、ATR、swing、区间、ADR、1 小时线）。
- `pa-brooks-strategy`: PA v1.0 的行为：尖峰与回调的判定、H／L 计数、过滤与纪律、订单意图、决策输出、人工计划一致性评估。

### Modified Capabilities

无。阶段 1 的七个能力不变；`scan` 仍只做数据校验。

## Impact

- **代码**：新增 `src/trading_agent/strategies/base.py`、`registry.py`、`pa_brooks.py`，新增 `src/trading_agent/features.py`；新增配置、文档、测试向量。不改阶段 1 的任何模块和表结构。
- **依赖**：无新增。
- **数据**：不读写运行库。
- **需要你确认的内容**（不阻塞本阶段，写在 `docs/strategy-spec.md`）：非 Spike 形态和 1 小时趋势的量化草案；确认之前它们保持关闭。
- **缺一份原始材料**：提示词包说 PA v1.0 整合自 `strategy_prompt_PA_ESNQ_5m.md`（v2026-11-22）。我没有这份文件，本提案只依据 Prompt 2 的文字。你手头有的话请提供，我用它核对 Prompt 2 没写细的地方。
