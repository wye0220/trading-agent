# Design

## Context

动机见 proposal.md。阶段 1 提供的输入：`context.visible_bars`、`hourly_asof`、`rth_day_ranges_asof`、`reference_levels_asof`，都按决策时点返回当时可见的数据；价格是整数 tick。

约束（来自 `CLAUDE.md` 和 Prompt 2）：策略是无 I/O 的纯函数；价格比较用整数 tick；Python 决定信号，Claude 只解释；阶段 1–3 验收后接口进入 `FROZEN_CONTRACT`；新增策略不得改引擎和扫描器。

Prompt 2 对 Spike 回调给了完整的数值规则；对其余形态、周期和 1 小时趋势只有描述，并明确要求"用户确认前，未定量的形态只作观察标签，不生成订单"。

## Goals / Non-Goals

**Goals:**

- 接口足够通用：阶段 3 用一个不同于 PA 的 Mock 策略（`MARKET_NEXT_OPEN`）也能跑通，不依赖 PA 的内部字段。
- Spike 回调的每条规则都有逐 bar 向量，多空镜像。
- 相同的快照和配置给出相同的结果。

**Non-Goals:**

- 不做成交模拟、风控手数、账本（阶段 3）。
- 不把未定量的形态做成"差不多"的规则。
- 不评估策略是否赚钱。

## Decisions

### 1. 范围只到 Prompt 2，分支 `phase-2-strategy-spi`

从 `phase-1-data-contract` 分出，不合并到 `main`，等用户确认。

### 2. 接口的形状

```
StrategyProvider
  strategy_family: str            version: str
  feature_requirements() -> FeatureRequirements
  initial_state() -> StrategyState
  on_bar(ctx: FeatureContext, prev: StrategyState) -> StrategyStepResult
  evaluate_pretrade_alignment(ctx, state, plan: TraderPlan) -> AlignmentResult
```

- `FeatureContext`、`OrderIntent`、`DecisionMetadata`、`StrategyStepResult`、`TraderPlan`、`AlignmentResult` 都是不可变的 dataclass。`FeatureContext` 只含数据，没有数据库连接或回调。
- `StrategyState` = `state_schema_version` + 一个只含 JSON 基本类型的字典。`to_json()` 的结果就是 `scan_events.strategy_state_json`；反序列化后继续运行必须得到同样的结果。
- `on_bar` 不修改传入的状态，返回新状态。
- `initial_state()` 是 Prompt 2 没有列出的补充：引擎需要一个起点。

*备选*：让插件自己持有状态。否决，回放、事故重放和并行影子都需要外部能保存和恢复状态。

### 3. `OrderIntent` 与目标描述

字段：`side`、`order_type`（`STOP_LIMIT_NEXT_BAR` | `MARKET_NEXT_OPEN` | `LIMIT_RETRACE`）、`trigger_ticks`、`limit_ticks`、`ttl_bars`、`stop_ticks`、`target`、`setup_id`、`evidence`。

`target` 只有三种：`ABSOLUTE_PRICE_TICKS`、`OFFSET_FROM_FILL_TICKS`、`INITIAL_RISK_MULTIPLE`，各带一个数值。构造时校验：类型不在这三种之内、数值不为正、止损在入场的错误一侧、限价在触发价的错误一侧，都直接报错。意图里没有任何可执行代码或策略名驱动的字段。

### 4. 特征是独立的纯函数模块，插件只读取结果

`features.py` 从一串已可见的 K 线算出特征；阶段 3 的"特征调度器"负责调用它并组装 `FeatureContext`。本阶段的测试直接调用这些函数。

- **EMA20**：用收盘价，`α = 2/21`，以第一根的收盘为种子。满 20 根之前为"不可用"。
- **ATR14**：Wilder 平滑。前 14 个真实波幅的简单平均为首值，之后 `ATR = (前值×13 + TR)/14`。满 14 个 TR 之前不可用。结果保留为分数形式的 tick，不四舍五入；与阈值比较时用精确的有理数运算。
- **序列的起点**：EMA 和 ATR 在同一实际合约的 5 分钟线上连续计算，含 ETH（ETH 只供预热）。换月后从新合约的数据重新起算，不跨合约接续。
- **swing**：左右各 2 根严格确认。高点要严格高于左右各 2 根的高点；等高不确认。右侧第 2 根可见之后才可用，`known_at` 取那一根的可用时点。
- **`rolling_20_range`**：最近 20 根已完成 5 分钟线（含当前根）的最高和最低。不足 20 根不可用。
- **`intraday_rth_range`**：当日 RTH 截至当前根的最高和最低。
- **ADR**：当前交易日之前 10 个 RTH 交易日里完整有效日的高低差均值，至少 3 个有效日，否则不可用。记录有效日数和日期。不含当天。
- **1 小时线**：最近 10 根已完结的 1 小时线，原样提供；方向判定是策略的事。

### 5. Spike 的判定

- **强趋势 K**（牛）：实体 ≥ 全幅的 50%，收盘在全幅的上 1/3 之内，最高价严格高于前一根的最高价。熊镜像。全幅为零的 K 不是强趋势 K。
- **Spike**：至少 2 根连续的同向强趋势 K，且"首根开盘到末根收盘"的方向幅度 ≥ 2.5 × ATR。
- **用哪个 ATR**：取 Spike 末根收线时的 ATR14。Prompt 2 写的是"当时已知 ATR"，这是字面上最直接的读法。它包含了 Spike 自身的大 K 线，会让门槛略高，属于偏保守的一侧。记入假设。
- **Spike 的延伸**：强趋势 K 的连续段结束后，后面的 K 只要还在创新极值（即使不够"强"），就继续抬高 `spike_extreme`，仍处在 Spike 阶段。
- **回调第 1 根**：Spike 之后第一根不再创新极值的 K。
- Spike 进行中出现反向 Spike，以新的为准，旧的作废。

### 6. 回调状态机

状态：`IDLE` → `SPIKE`（已确认，等回调）→ `PULLBACK` → 结束（`INVALIDATED` | `TREND_RESUMED`）回到 `IDLE`。

- **回撤比例** = 回调朝反方向走过的最大距离 ÷（Spike 极值 − Spike 首根开盘）。用整数 tick 的交叉相乘比较，不用浮点。
- **失效**：ES／MES 回撤 > 75% 或回调 > 20 根；NQ／MNQ 回撤 > 60% 或回调 > 12 根。等于阈值仍有效。达到失效的那一根优先作废，同根不发信号。
- **趋势恢复**：回调期间某根收盘越过 Spike 极值，状态机以 `TREND_RESUMED` 结束，不再延续计数。
- **反向腿**（牛 Spike）：某根的最低价严格低于前一根的最低价，开启一条新腿。
- **H 信号**：新腿开启之后、晚于腿首根的第一根"收阳且收在上半部、并通过通用质量检查"的 K，标为 H1；下一条新腿之后的标 H2，再下一条标 H3。每条腿只计一次，同一根不重复编号。H3 之后不再发信号，等失效或恢复。
- 熊 Spike 全部镜像：新腿由最高价严格高于前一根开启，信号 K 收阴且收在下半部，标 L1–L3。
- **通用质量检查**：实体 ≥ 全幅的 20%，全幅 ≤ 3 × ATR，全幅不为零。

### 7. 过滤器链与豁免

按固定顺序评估，记录全部证据和第一个拦截原因。每个形态在配置里登记豁免哪些过滤器。

| 顺序 | 过滤器 | Spike 回调 |
| --- | --- | --- |
| 1 | H1／L1 拦截 | 豁免 |
| 2 | 尖峰内禁止逆势 | 适用（Spike 回调本身顺势，不会被拦） |
| 3 | 逆 1 小时方向 | 豁免 |
| 4 | 5 分钟区间低买高卖 | 豁免 |
| 5 | 衰竭 | 豁免 |
| — | 尾盘 30 分钟不追单 | 适用 |
| — | 两次止损停手 | 适用 |
| — | 特征不可用（ATR 等） | 适用，输出 `WAIT` |

五个过滤器都按 Prompt 2 的文字实现并单独测试，但 v1.0 只有 Spike 回调启用，所以真正会拦它的只有后三项。过滤器 3 依赖 1 小时方向，而方向规则未定量，v1.0 的 `htf_direction` 恒为 `UNKNOWN`；非豁免的形态遇到它一律 `WAIT`。

**尾盘**：Prompt 2 说"急拉急跌"和"追单"的定义未配置时保守拦截顺势追价候选。Spike 回调属于顺势，所以信号 K 收线时刻在 RTH 收盘前 30 分钟以内的，一律拦截，原因 `TAIL_WINDOW_UNCONFIGURED`。提前收盘日按当日收盘时刻算。

### 8. 两次止损停手（纸面）

`FeatureContext` 带一份只读的"本交易日已完结纸面交易"列表（由阶段 3 的引擎提供），每条有 `setup_id`、退出原因和净盈亏的符号。插件据此在状态里维护连续止损计数：

- 止损出场计 1 次。
- 达到 2 次之前，只有已完结的净盈利交易能清零；亏损或保本出场不清零。
- 达到 2 次后，本交易日不再产生新的 `SETUP_VALID`，原因 `TWO_STOPS_HALT`。
- 交易日切换时清零。

真实账户的跨策略停手是阶段 3、4 的账户级风控，不在插件里。

### 9. 订单意图的数值

- 类型 `STOP_LIMIT_NEXT_BAR`，`ttl_bars=1`。
- 做多：触发价 = 信号 K 最高价 + 1 tick；限价 = 触发价 + 限价偏移（默认 1 tick，可配置）。做空镜像。
- 保护止损：Spike 回调用回调极值外 1 tick，发信号时冻结。
- 目标：RTH 且 ADR 可用时，`OFFSET_FROM_FILL_TICKS` = 0.40 × ADR 向上取整为整数 tick，至少 1；否则 `INITIAL_RISK_MULTIPLE = 2`。14:30 后切换 2R 的选项默认关闭。
- `setup_id` = `sha256(strategy_family | contract_id | setup | signal_bar_end_utc | version)` 的前 16 位十六进制。

Prompt 2 提到限价"同时服从风险价格边界"，但没有给出边界的定义。本阶段不实现，记入假设，留给阶段 3 的风控。

### 10. 周期与其余形态

- 周期输出：`BULL_SPIKE`、`BEAR_SPIKE`、`BULL_MICROCHANNEL`、`BEAR_MICROCHANNEL` 有客观定义，可以输出。微通道：连续 N 根（草案 N=5）不破前一根低点（牛）或高点（熊）。
- `WIDE_BULL_CHANNEL`、`WIDE_BEAR_CHANNEL`、`TRADING_RANGE` 是描述性的，v1.0 不输出，遇到时为 `UNKNOWN`。`UNKNOWN` 不当作交易区间处理。
- 形态优先级表完整登记（高潮反转 > Spike 回调 > MTR > 楔形 > 失败突破 > 双顶／双底 > 更低高点／更高低点 > 牛旗／熊旗 > H2／L2 > H1／L1），每项有 `enabled` 开关。v1.0 只有 Spike 回调为 `true`。
- 试图在配置里启用一个没有实现的形态，加载时报错，而不是静默忽略。

`docs/strategy-spec.md` 为每个未启用的形态、宽通道、交易区间、交易区间日和 1 小时方向各写一段量化草案：突破参照、摆动结构、回调腿重置、等高等低容差、确认时点、失效条件、多空镜像、边界测试。草案是给用户看的提议，不是代码。

### 11. 人工计划一致性

`evaluate_pretrade_alignment` 返回 `ALIGNED` | `DEVIATES` | `UNKNOWN`，加标记和证据：

- 计划方向与当前有效 Spike 相反 → `DEVIATES`，标记 `COUNTERTREND_BEFORE_CONFIRMATION`。
- 计划与当前 `SETUP_VALID` 的方向一致且入场、止损和它的意图相容 → `ALIGNED`，附 `setup_id`。
- 没有有效的结构可供判断 → `UNKNOWN`。
- 计划没有止损 → 标记 `NO_STOP`（结论另算）。

### 12. 决策输出与中文短报

`DecisionMetadata` 含 Prompt 2 第 6 节列出的全部字段。另有一个纯函数把结果渲染成中文：先报新信号及状态，再报周期、最新 K 线、参考价与下一确认条件，末尾附固定的免责声明。持仓一段由阶段 4 补上。

### 13. 测试向量

YAML，每个文件一个场景：品种、配置覆盖项、一串 K 线（纽约时间、点数价格）、可选的特征覆盖（如固定 ATR，避免每个向量都铺 20 根预热 K）、逐根的期望（状态、标签、订单意图的各个数值、拦截原因）。

多空镜像由运行器自动生成：把一个做多场景按某个价位翻转，期望的方向和标签相应互换，再跑一遍。这样每条规则的镜像不靠手写保证。

特征数学（EMA、ATR、swing、ADR）另有不带覆盖的单元测试。

## Risks / Trade-offs

- **Spike 的 ATR 口径是我的解读** → 写入假设，向量里有一条正好在 2.5×ATR 边界上的用例；用户若选另一种读法，只改一处。
- **没有原始的 `strategy_prompt_PA_ESNQ_5m.md`** → 只依据 Prompt 2。凡是 Prompt 2 没写细而我补了定义的地方，都列在假设里。
- **不在真实数据上跑** → 本阶段无法知道信号频率是否合理。这是有意的：先定盲测边界（阶段 5），再看结果。
- **v1.0 只有一种会下单的形态** → 信号可能很少。这是提示词包的设计，其余形态要等量化草案确认。
- **接口在阶段 3 才被引擎真正使用** → 可能发现缺字段。阶段 1–3 验收前契约未冻结，可以调整。

## Open Questions

- 量化草案里的各个阈值，等用户看过 `docs/strategy-spec.md` 后确认或修改。确认之前相关形态保持关闭，不影响本阶段的任务。
