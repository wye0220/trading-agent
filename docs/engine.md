# 引擎核心（阶段 3）

术语见 `docs/glossary.md`。代码在 `src/trading_agent/engine/`。引擎只提供函数和事务封装，`scan`（阶段 4）和 `replay`（阶段 5）在它外面包一层。它不依赖任何具体策略的内部字段，只通过 `StrategyProvider` 接口与策略交互。

| 模块 | 内容 |
| --- | --- |
| `fills.py` | 成交模型（纯函数）和引擎版本号 |
| `runner.py` | 逐步运行器、回放、冻结 |
| `risk.py` | 每手风险、手数、结算、真实账户的预算与停手 |
| `versions.py` | 版本元组 |
| `llm.py` | 给模型的上下文、输出校验、解释请求队列 |
| `config.py` | `config/risk.yaml`、`config/engine.yaml` 的加载 |

引擎版本：`fill-v1.0`。改成交规则或事件顺序必须改它。

---

## 一、时间

### 四个时刻

对一根在 `T` 收盘的 5 分钟信号 K：

| 时刻 | 定义 |
| --- | --- |
| 信号 K 收盘 `T` | `bar_end_utc` |
| 数据可用 `A` | 这根 K 当时可见的 revision 的 `available_at_utc` |
| 决策时点 `t` | 引擎处理这根 K 的时刻，也是挂单的激活时刻。回放里取 `A` 加配置的延迟；盘中取实际运行时刻 |
| 过期时刻 `X` | `T + ttl_bars × 周期`，并且不晚于当日的截止强平时刻 |

规则：

- 决策只用 `bar_end_utc ≤ t` 且 `available_at_utc ≤ t` 的数据，取当时可见的最高 revision。
- 之后才到的数据或修订只影响之后的决策。已产生的信号、订单、成交不回写。
- 激活之前的触价不可用。
- `t ≥ X` 时不生成订单，记 `MISSED_LATE_SIGNAL`。

例：09:35 收盘的信号在 09:35:02 被处理，有效 1 根。订单 09:35:02 激活，09:40:00 过期。09:35:01 的触价不可用，09:35:10 的可用。到 09:40 之后才处理，就不再挂单。

### 时钟只前进

每个运行（`engine_runs`）有三个水位：

- `watermark_utc`：最近一次决策时点。新的步进早于它时抛 `ClockError`，运行库不变。
- `last_bar_end_utc`：最近处理过的信号 K。
- `market_watermark_utc`：成交模型已经消费到的子 K 结束时刻。

### 子 K 与激活时刻

成交模型吃的是一串**子 K**：当日所选来源有 1 分钟原始线时用 1 分钟线，否则用 5 分钟线本身。对一张在 `t` 激活的订单，子 K `[s, e)`：

| 关系 | 处理 |
| --- | --- |
| `e ≤ t`，完全在激活之前 | 对这张订单不存在 |
| `s ≥ t`，完全在激活之后 | 正常使用 |
| `s < t < e`，跨着激活时刻 | 照常按规则成交，入场成交带 `AMBIGUOUS_ENTRY` |

只有 5 分钟线时，下一根 K 必然跨着激活时刻，所以那些时段的入场都带这个标记。回放里激活时刻是"收盘 + 5 秒"，第一根 1 分钟线也跨着它，在第一分钟内触发的入场同样带标记。

`MARKET_NEXT_OPEN` 是例外：有 1 分钟线时，它跳过跨着激活时刻的那一根，在下一根的开盘成交，不带标记。

---

## 二、成交模型

规则按做多叙述，做空镜像（代码用定向坐标，同一套逻辑处理两个方向）。只有开高低收时，已知的先后只有"开盘最先、收盘最后"。

### 订单状态

`ARMED → TRIGGERED → FILLED`，或 `EXPIRED`、`CANCELLED`。

### `STOP_LIMIT_NEXT_BAR`（触发价 `P`，限价 `L ≥ P`）

| 情形 | 结果 | 成交方式 |
| --- | --- | --- |
| 开盘 ≥ `P` 且开盘 ≤ `L` | 在开盘价成交 | `open` |
| 开盘 > `L`，同一子 K 最低价 ≤ `L` | 触发；回落到限价，按 `L` 成交 | `gap_return` |
| 开盘 > `L`，最低价 > `L` | `TRIGGERED`，未成交，带入后面的子 K | — |
| 开盘 < `P`，最高价 ≥ `P` | 盘中触发，按 `min(P + 入场滑点, L)` 成交 | `intrabar` |
| 最高价 < `P` | 不触发 | — |
| 已 `TRIGGERED`，后面的子 K 最低价 ≤ `L` | 开盘 ≤ `L` 按开盘成交，否则按 `L` | `open` 或 `gap_return` |
| 到过期时刻仍未成交 | `EXPIRED` | — |

成交价在任何情况下都不越过限价，不管入场滑点配多大。

### `MARKET_NEXT_OPEN`

激活后第一个可用的开盘价加入场滑点。只有 5 分钟线时，用跨着激活时刻的那根的开盘，带 `AMBIGUOUS_ENTRY`。

### `LIMIT_RETRACE`（限价 `L`，价格回落到它才买）

有效期内某根子 K 的最低价 ≤ `L` 时按 `L` 成交，不给价格改善。成交方式 `limit`。

### 目标定价

成交后把目标描述换成价格：

| 描述 | 价格（做多） |
| --- | --- |
| `ABSOLUTE_PRICE_TICKS` | 数值本身 |
| `OFFSET_FROM_FILL_TICKS` | 成交价 + 数值 |
| `INITIAL_RISK_MULTIPLE` | 成交价 +（成交价 − 止损）× 倍数，向离入场更远的方向取整到整数 tick |

成交价已经越过止损，或算出的目标不在入场的顺方向一侧时，仓位立即按成交价离场，带 `INVALID_AFTER_FILL`。

### 成交之后

保护止损和目标立即生效，一个成交则另一个撤销。

**入场当根：**

| 情形 | 结果 |
| --- | --- |
| 触及止损（盘中触发、回落成交、回落限价单） | 止损出场，`AMBIGUOUS_BAR` |
| 开盘成交，只触及止损 | 止损出场，无标记（开盘最先，止损必在入场之后） |
| 开盘成交，止损和目标都触及 | 止损出场，`AMBIGUOUS_BAR` |
| 开盘成交或盘中触发，只触及目标 | 目标出场（高于入场价的价格必然出现在入场之后） |
| 回落成交或回落限价单，最高价 ≥ 目标 | **不算**，仓位保持，带 `AMBIGUOUS_PATH`（最高价可能在成交之前） |

**之后的子 K：**

| 情形 | 结果 |
| --- | --- |
| 开盘已越过止损 | 按开盘价减止损滑点出场，`GAP_THROUGH_STOP` |
| 开盘已越过目标 | 按目标价出场（不给价格改善） |
| 只触及止损 | 按止损价减止损滑点出场 |
| 只触及目标 | 按目标价出场 |
| 都触及 | 止损出场，`AMBIGUOUS_BAR` |

先后不明的触价不会被记成盈利。有 1 分钟线时，止损和目标落在不同分钟，先后就是确定的。

目标按"触及"口径：价格到达目标价即成交。阶段 5 另报"穿价 1 tick 才成交"的敏感性。

### 截止强平

截止时刻 = RTH 收盘前 `flatten_minutes_before_close` 分钟（默认 5：常规日 15:55 ET，半日市 12:55 ET）。

- 起始时刻不早于截止时刻的第一根子 K 到来时：未成交的入场单 `CANCELLED`（原因 `CUTOFF`）；持仓按那根的开盘价减止损滑点平仓，退出原因 `FORCED_FLAT`。
- 截止之后不生成新订单（`AFTER_CUTOFF`）。
- 截止之后没有行情：仓位带 `UNPRICEABLE` 和 `UNABLE_TO_FLATTEN`，写一条 `CRITICAL` 告警；等第一个有效价格（可能是次日）再按上面的规则平仓。

### 反手

同 scope 不双向持仓。持仓期间一张反向订单成交时：

- 旧仓在同一个模型价格上平掉，退出原因 `REVERSAL`；新仓同时开出；各记各的费用。
- 旧仓的止损若在这根子 K 上也被触及，旧仓按止损出场并带 `AMBIGUOUS_BAR`，新仓照开。
- 旧仓的目标若在这根上也被触及，仍按反手价出场，带 `AMBIGUOUS_PATH`。
- 反向订单没成交，旧仓照常管理。

同向已有仓位时新订单不加仓：成交时被撤销（`POSITION_OPEN_SAME_SIDE`）；运行器在生成订单前就会拦下。

### 缺数据

- 有效期内没有任何子 K：订单到期 `EXPIRED`，带 `DATA_GAP_IN_WINDOW`。
- 不插值，不造成交。
- 订单和仓位绑定实际合约，只吃该合约的子 K。日内强平保证不跨换月。

### 一根子 K 内的处理顺序

1. 截止强平和截止撤单。
2. 旧仓位的开盘跳空。
3. 挂单（含反手和入场当根的止损、目标）。
4. 旧仓位的盘中止损与目标。

### 标记一览

| 标记 | 出现在 | 含义 |
| --- | --- | --- |
| `AMBIGUOUS_ENTRY` | 订单、仓位、交易 | 入场所依据的子 K 跨着激活时刻 |
| `AMBIGUOUS_BAR` | 交易 | 止损与目标（或入场与止损）先后不明，按止损处理 |
| `AMBIGUOUS_PATH` | 仓位、交易 | 有一次目标触及因先后不明没有被采信 |
| `GAP_THROUGH_STOP` | 交易 | 跳空穿过止损，亏损超过计划风险 |
| `INVALID_AFTER_FILL` | 交易 | 成交价已越过止损或目标方向不对，立即离场 |
| `DATA_GAP_IN_WINDOW` | 订单 | 有效期内没有行情 |
| `UNPRICEABLE`、`UNABLE_TO_FLATTEN` | 仓位、交易 | 截止之后没有行情，无法按时平仓 |
| `FEES_NOT_CONFIGURED` | 交易 | 费用未配置，按 0 计 |
| `RESEARCH_RISK_UNIT_NOT_SET` | 交易 | 研究风险单位未配置，只有按初始风险计的 R |

订单的结束原因：`FILLED`、`TTL`（到期）、`CUTOFF`（截止撤单）、`POSITION_OPEN_SAME_SIDE`。
仓位的退出原因：`STOP`、`TARGET`、`FORCED_FLAT`、`REVERSAL`。

### 测试向量

`tests/vectors/fills/*.yaml`，32 个。每个向量给出订单、激活时刻、一串子 K 和期望的事件序列，正向跑一遍，再按价格翻转跑一遍镜像。

---

## 三、逐步运行器

`runner.step(settings, con, run_id, strategy, as_of_utc)` 在一个事务里（先拿写锁）做完一步：

1. 加载该 scope 的版本元组，这一步内不变。
2. 找出"上次处理之后、此刻已可见"的信号 K，**按时间顺序逐根处理**。通常只有一根；睡眠或停机之后会有几根，落下的那些照样喂给策略以保持状态连续。
3. 对每一根：
   - 把成交模型推进到这根 K 收盘，结果写进账本。
   - 用当时可见的数据组装 `FeatureContext`（含本交易日已完结的纸面交易），调用 `strategy.on_bar`。
   - 若 `SETUP_VALID`，依次检查，先命中的生效：

     | 检查 | 结果 |
     | --- | --- |
     | 决策时点不早于过期时刻 | 不挂单，`MISSED_LATE_SIGNAL` |
     | 已到截止时刻 | `BLOCKED`，`AFTER_CUTOFF` |
     | scope 被冻结或数据被阻止（在事务内重读） | `BLOCKED`，`FROZEN:<原因>` 或 `DATA_BLOCKED:<原因>` |
     | 同向已有仓位 | `BLOCKED`，`POSITION_OPEN_SAME_SIDE` |
     | 还有未结束的挂单 | `BLOCKED`，`ORDER_ALREADY_WORKING` |
     | 以上都没有 | 纸面风控（固定 1 手，`PASS`），生成 `ARMED` 订单，`paper_action = QUEUE_ENTRY` |
   - 写一条 `scan_events`（含完整结果和策略状态）。
4. 更新策略状态和水位。带 `version` 校验。

落下的旧 K 的信号多半已经过期，按 `MISSED_LATE_SIGNAL` 处理，不会变成当前的纸面入场。

**幂等**：同一个运行、同一根信号 K 重跑是空操作，返回第一次存下的结果（`status = ALREADY_PROCESSED`）。订单、成交、仓位、交易的 ID 由 `run_id` 和 `setup_id` 确定性生成。

**冻结**：`runner.set_freeze` 登记冻结或解除，按原因（`DATA`、`MANUAL`、`DAILY_LOSS`、`MONITOR`）分别生效、分别解除。`risk_domain` 可以是某个运行的风险域，或 `*`。`step` 在提交订单前重读，所以"冻结与入场同刻"时冻结胜出。阶段 9 在此之上提供 `freeze` 命令。

**两个循环**：收线主循环就是 `step`。异步中断（人工 `plan`、紧急冻结、行情断流）各自在自己的事务里写状态，都带 `version` 校验；`step` 一开始就拿写锁，所以它读到的冻结状态和它提交的订单之间不会被插入。

`runner.replay(settings, con, scope, strategy, start_day, end_day)` 按每根信号 K 的可用时点依次调用 `step`。阶段 5 在它上面加报告。

**真实数据的保护**：`replay` 发现区间内有非合成数据时，只有同时满足三条才运行：调用方显式允许、已配置 `dev_visible_through`、回放区间不超过它。否则抛 `ReplayRefused`。

---

## 四、风控

### 每手计划风险

```
每手计划风险 = 止损 tick 数 × tick 价值 + 每手往返费用 + 滑点预算 × tick 价值
```

费用配置成 `per_side`（每边）或 `round_trip`（往返）二选一。成交价里已经含了入场和止损滑点，费用里不重复算。

例：ES 止损 16 tick，往返费用 5 美元，滑点预算 1 tick → 16 × 12.5 + 5 + 12.5 = 217.5 美元。

### 两个风险域

| | 纸面 | 真实账户 |
| --- | --- | --- |
| 键 | scope、`run_id`、交易日 | `account_id`、交易日 |
| 手数 | 固定（默认 1） | `floor(单笔预算 ÷ 每手风险)`，再受最大手数和当日剩余风险约束 |
| 预算门禁 | 不经过 | 经过 |
| 两次止损 | 策略自己的纪律 | 账户级，所有策略共享 |
| 日损 | 只记账 | 到限后拦新入场 |

两边互不影响：纸面止损不改账户记录。

### 人工计划的核验

`risk.check_manual_plan(...)` 是纯函数，按顺序检查，先命中的原因生效：

| 原因码 | 条件 |
| --- | --- |
| `BUDGET_NOT_APPROVED` | 账户限额没填全，或 `approved` 不是 `true` |
| `NO_STOP` | 计划没有止损 |
| `RISK_UNKNOWN_OR_STALE` | 账户状态缺失、不完整，或快照超过时效 |
| `ACCOUNT_HALTED_TWO_STOPS` | 账户当日已停手 |
| `DAILY_LIMIT_HIT` | 当日已实现亏损到限，或剩余风险不够 1 手 |
| `MIN_SIZE_OVER_BUDGET` | 单笔预算连 1 手都不够 |
| `RISK_OVER_BUDGET` | 请求的手数超过规则允许的 |

`risk.reserve(...)` 把核验和预留放在同一个事务里：先拿写锁，再读账户状态；调用方预检时读到的版本与库中不一致时抛 `StaleVersion`，状态不变。同一个计划重复预留是空操作。`risk.release(...)` 释放预留。

`risk.record_account_exit(...)` 登记真实账户的已完结交易，按券商成交或订单 ID 去重。止损出场计 1 次；达到上限后当日停手，换策略不解除；停手之前只有净盈利的交易把计数清零，亏损和保本不清零。

这些函数由阶段 4 的 `plan`、`watch` 和人工成交导入调用。

### 纸面交易的结算

每笔已完结交易（`paper_trades`）记录：tick 盈亏、毛盈亏、费用、净盈亏、净盈亏的符号，以及两种 R：

- `r_initial`：净盈亏 ÷（初始止损距离 × tick 价值 × 手数）。
- `r_research`：净盈亏 ÷ 预登记的固定研究风险单位。没配置时为空，带 `RESEARCH_RISK_UNIT_NOT_SET`。

费用没配置时按 0 计，带 `FEES_NOT_CONFIGURED`。

---

## 五、版本绑定

`versions.current(scope, strategy, risk_cfg, release_con)` 返回：

| 字段 | 来源 |
| --- | --- |
| `strategy_family`、`strategy_version` | 策略对象 |
| `risk_version` | `config/risk.yaml` 内容的摘要，改任何一项都变 |
| `prompt_version`、`memory_version`、`model_id` | 调用方传入，现在为空 |
| `engine_version` | `fill-v1.0` |
| `release_id`、`manifest_hash` | 发布库的 active 指针；没有时为 `UNRELEASED` |

每一步开始时加载一次。新订单记下当时的元组；仓位沿用它的订单的，已完结交易沿用它的仓位的。所以持仓期间版本变了，旧仓位仍记旧版本，之后的新订单记新版本。

---

## 六、给模型的上下文与输出

本阶段不调用模型，只提供三样东西。

### 上下文

`llm.build_scan_context(...)` 的内容：scope、时刻、信号状态、风控状态、订单意图的数值、决策的核心字段、结构状态、证据、纸面仓位摘要、版本元组。**没有原始 K 线序列**；证据里只有信号 K 这一根。

token 按字符数保守估算（每 token 2 个字符）。超出预算时按固定顺序移除：过滤器结论 → 目标依据 → 质量检查数值 → 已启用形态列表 → 参考价位 → 纸面仓位 → 整个证据 → 整个结构状态。移除了什么记在 `trimmed` 里。只剩核心字段仍超预算时抛 `ContextTooLarge`，不静默截断。

预算在 `config/engine.yaml` 的 `token_budgets`。

### 输出 schema

`schemas/` 下六个 JSON Schema（Draft 2020-12），都禁止未定义的字段：

| Schema | 用途 | 要点 |
| --- | --- | --- |
| `ScanExplanation` | 解释一次扫描 | 免责声明是固定文本 |
| `PretradeCheck` | 人工计划预检 | 结论只能是 `ALIGNED`、`DEVIATES`、`BLOCKED_BY_RISK` |
| `SessionReview` | 日复盘 | 事实、证据不足、事后信息分开；归因用固定的五类 |
| `Candidate` | 改动候选 | `exact_diff` 只能有一项，目标只能是策略配置、规则开关、模板文件 |
| `MemoryCandidate` | 记忆候选 | `statement` 全是结构化字段，`subject` 等只接受小写标识符，写不进自由文本 |
| `ReleaseReview` | 发布前审阅 | 建议只能是"可交给用户决定"或"未就绪"，不能是批准 |

`llm.validate_output(...)` 校验不过时返回 `False` 并写一条 `WARN` 告警（`LLM_SCHEMA_INVALID`）。调用方丢弃该输出。信号、订单、成交、盈亏不受影响。

### 解释请求队列

`explanation_requests` 表。`enqueue_explanation` 连同快照 ID 入队；`fail_explanation` 登记一次失败，未到上限时留在队列里（快照不变），达到 `explanations.max_retries`（默认 3）时转入 `dead_letter_queue` 并写一条 `WARN` 告警；`complete_explanation` 标记完成。

---

## 七、配置

`config/risk.yaml`：费用口径与金额、入场和止损滑点、滑点预算、纸面手数、研究风险单位、账户限额。**金额类字段默认留空**，表示未配置：

- 费用留空：按 0 计，结果带 `FEES_NOT_CONFIGURED`。
- 研究风险单位留空：只有 `r_initial`。
- 账户限额留空或 `approved: false`：人工计划一律 `BUDGET_NOT_APPROVED`。纸面评测不受影响。

`config/engine.yaml`：截止强平提前分钟数、回放的激活延迟、解释请求最大重试次数、各类调用的 token 预算。

---

## 八、假设

1. **跨着激活时刻的入场默认成交并标记**（`AMBIGUOUS_ENTRY`）。提示词包对入场歧义给了"记 MISSED 或 AMBIGUOUS_ENTRY"两种说法；选前者的话，只有 5 分钟线的时段将没有任何成交。
2. **目标和回落限价单不给价格改善**：跳空越过目标按目标价成交，回落限价单按限价成交。
3. **开盘成交后同根只触及止损不标歧义**：开盘最先，先后是确定的。
4. **反手那根上旧仓的目标不采信**，按反手价出场。
5. **截止强平用截止时刻那根子 K 的开盘价**。
6. **`INITIAL_RISK_MULTIPLE` 的目标向离入场更远的方向取整**。
7. **回放的决策时点**是信号 K 第一个 revision 的可用时点，加 `replay_activation_delay_seconds`（默认 0）。
8. **补算旧 K 时的可见性**：落下的 K 用它自己的可用时点去查背景数据，但 K 线的 revision 取的是补算当时可见的。
9. **每个 scope 的每个运行同一时间最多一张挂单、一个仓位**。
10. **纸面没有日损上限**，只有策略自己的两次止损停手。
11. **token 估算**是字符数除以 2，偏保守。
12. **有版本元组但没有发布清单**：发布库有 active 指针时只取它的 `release_id` 和 `manifest_hash`，各组成部分仍取当前代码和配置。阶段 9 定义清单后再对齐。
13. **限价"服从风险价格边界"**（Prompt 2）仍未实现：没有给出定义。
