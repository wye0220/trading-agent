# 阶段 2 汇报：策略插件接口与 PA v1.0

日期：2026-11-22。分支：`phase-2-strategy-spi`（从 `phase-1-data-contract` 分出，未合并）。变更：`openspec/changes/archive/2026-11-22-add-strategy-spi-pa-brooks/`。

## 1. 完成项与未完成项

**完成**

- 策略插件接口：`StrategyProvider`、`FeatureContext`、`StrategyState`、`OrderIntent`、`TargetSpec`、`DecisionMetadata`、`StrategyStepResult`、`TraderPlan`、`AlignmentResult`，以及策略注册表。一个与 PA 无关的模拟策略只靠这套接口就能跑通。
- 通用因果特征：EMA20、ATR14、swing、滚动 20 根区间、当日 RTH 区间、ADR、1 小时线背景，以及组装 `FeatureContext` 的函数。
- PA v1.0 插件：强趋势 K、Spike 的识别与延伸、回调状态机、失效与趋势恢复、H1–H3 / L1–L3 计数、信号 K 质量检查、五个过滤器及豁免表、尾盘不追单、两次止损停手、订单意图、周期输出、决策元数据、中文短报、人工计划一致性评估。
- 策略配置及其校验；启用未实现的形态会加载失败。
- 25 个逐 bar 向量，每个正向和镜像各跑一遍。
- `docs/strategy-spec.md`：接口、特征、PA v1.0 规格、13 项量化草案、20 条假设、5 条研究排除项。

**未完成**

没有未完成的任务。但有三件事要说明：

- **只有 Spike 回调会生成订单。** 其余九类形态、宽通道、交易区间、交易区间日和 1 小时方向都没有实现，只有草案，等你确认。
- **策略没有接进 `scan`，也没有在真实数据上运行过。** 这是有意的：没有设开发可见截止日，在全部历史上看策略结果会污染以后的盲测。所以信号频率、盈亏一概未知。
- **没有原始的 `strategy_prompt_PA_ESNQ_5m.md`。** 只依据 Prompt 2 的文字。

## 2. 产物路径

- 代码：`src/trading_agent/strategies/base.py`、`registry.py`、`pa_brooks.py`，`src/trading_agent/features.py`
- 配置：`config/strategies/pa-esnq-5m-v1.0.yaml`
- 文档：`docs/strategy-spec.md`，`docs/glossary.md`（新增"策略"一节），`README.md`
- 测试：`tests/test_strategy_spi.py`、`tests/test_features.py`、`tests/test_pa_brooks.py`、`tests/vector_runner.py`、`tests/vectors/strategy/pa_brooks/*.yaml`

阶段 1 的模块、表结构和测试都没有改动。

## 3. 执行的命令和真实结果

| 命令 | 结果 |
| --- | --- |
| `uv run pytest` | 284 passed（阶段 1 的 127 项加阶段 2 的 157 项） |
| 25 个向量 × 正向、镜像、逐根状态往返 | 全部通过 |

## 4. 新增假设及写入位置

全部在 `docs/strategy-spec.md` 第五部分，共 20 条。最值得你看的三条：

1. **Spike 的 ATR 口径**（假设 1）：取 Spike 末根收线时的 ATR。它包含 Spike 自身的大 K 线，门槛略偏高。
2. **被拦截的信号消耗标签**（假设 7）：H1 被尾盘或停手拦截后，下一条腿的是 H2。
3. **尾盘一律拦截**（假设 12）："急拉急跌"和"追单"没有客观定义，按 Prompt 2 的保守做法，收盘前 30 分钟内的 Spike 回调信号全部拦截。

与原计划不同的一处：EMA 和 ATR 原计划用精确分数，实施时改为 1e-9 tick 的定点整数，因为精确分数在长序列上分母会无限增长。规格和设计文档已同步改写。

## 5. 下一阶段可复现的输入

- `trading_agent.strategies.registry.default_registry().get("pa_brooks")`
- `trading_agent.features.build_context(...)`：阶段 3 的特征调度器用它把阶段 1 的 as-of 查询结果组装成 `FeatureContext`。
- `tests/vector_runner.py`：阶段 3 的成交向量可以沿用同样的 YAML 写法。

阶段 3 之前需要你处理的事：

- 看 `docs/strategy-spec.md` 第四部分的量化草案，确认、修改或否决。不确认也不妨碍阶段 3，只是策略仍然只有 Spike 回调一种形态。
- 手头若有 `strategy_prompt_PA_ESNQ_5m.md`，请提供。
- 阶段 5 回放之前，必须定下开发可见截止日。
