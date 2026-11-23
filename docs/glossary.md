# 术语表

字段名、枚举和命令用英文，说明用中文。本表是 `CLAUDE.md`「统一口径」一节的展开。

## scope

四维隔离键 `strategy_family:instrument_root:session_type:timeframe`，例如 `pa_brooks:ES:RTH:5m`。每个 scope 有独立的账本、配置、候选队列、α 预算和版本指针。

| 维度 | 含义 | 首版取值 |
| --- | --- | --- |
| `strategy_family` | 策略家族 | `pa_brooks` |
| `instrument_root` | 品种根代码，须在 `config/instruments.yaml` 登记 | `ES`（`NQ`、`MES`、`MNQ` 已登记未启用） |
| `session_type` | `RTH` 或 `ETH` | `RTH` |
| `timeframe` | K 线周期 | `5m` |

## 时间与时段

- **存储**：一律 UTC。数据库里的时刻是 UTC epoch 秒（整数）；CSV 和报告里是 ISO 8601（`Z` 结尾）。
- **展示**：`America/New_York`（ET）。
- **RTH**：纽约时间 09:30–16:00。提前收盘（如 13:00）和休市日以锁定版本的 `exchange_calendars` `XNYS` 日历为准。
- **ETH**：同一交易日内 RTH 之外的 Globex 时段，即前一日 18:00 ET 至当日 17:00 ET 中除去 RTH 的部分。17:00–18:00 ET 是每日维护时段，没有交易。
- **交易日（`trading_day`）**：18:00 ET 起算。周日 18:00 之后的夜盘属于周一交易日。
- **休市日的 Globex 交易**（如 MLK 日的缩短时段）：没有 RTH，这些 K 线归入下一个交易日的 ETH 背景。

## 价格

- **整数 tick**：价格在内部一律以最小变动价位的整数倍存储和比较。ES 的 tick 是 0.25 点，5000.25 存为 `20001`。
- 不能被 tick 整除的来源价格记 `OFF_TICK_PRICE`，不四舍五入。

## 合约

- **`contract_id`**：实际合约的唯一标识，`<根代码><月份字母><四位年份>`，如 `ESH2027`。来源里的短代码（`ESH7`）在入库时规范化。
- **到期日**：合约月的第三个星期五。
- **换月规则**：版本化，登记在 `config/instruments.yaml`。现用 `roll-v2`：到期日前第 6 个日历日起换月；那天是星期六，所以实际从到期周的星期一交易日起（含周日晚上开始的夜盘）切到下一季月。`roll-v1`（前第 8 个日历日，星期四）已停用，定义保留。
- 成交和盈亏始终用实际合约，不做连续合约价格调整。

## K 线的时间字段

| 字段 | 含义 |
| --- | --- |
| `bar_start_utc` / `bar_end_utc` | K 线覆盖区间的起止 |
| `available_at_utc` | 这根 K 线（这个 revision）最早可被系统看到的时刻 |
| `available_at_estimated` | `available_at_utc` 是否为估计值。历史文件没有到达时刻，按「收盘 + 固定延迟」估计，标 `true`；Webull 采集用真实取数时刻，标 `false` |
| `known_at_utc` | 派生结果可知的时刻，取全部依赖中最晚的 `available_at_utc` |
| `revision` | 同一来源、合约、周期、起始时刻的第几个版本，从 1 起。内容变化才产生新 revision，旧版本保留 |

决策只能使用 `bar_end_utc <= t` 且 `available_at_utc <= t` 的数据，并使用当时已可见的最高 revision。

## 运行模式

`fixture`、`historical_replay`、`pre_eval_shadow`、`live_shadow`、`paper`、`manual_actual`。

- **`FIXTURE`**：合成行情的标记。随数据进入快照和报告。合成结果只用于验收，不可晋级。

## 版本元组

`strategy_family`、`strategy_version`、`risk_version`、`prompt_version`、`memory_version`、`engine_version`、`model_id`。

## 状态枚举

| 字段 | 取值 |
| --- | --- |
| `signal_status` | `WAIT` \| `SETUP_VALID` \| `DATA_ERROR` |
| `risk_status` | `NOT_EVALUATED` \| `PASS` \| `BLOCKED` |
| `paper_action` | `NONE` \| `QUEUE_ENTRY` \| `MANAGE_ONLY` \| `EXIT` |

仓位、候选和发布另有状态机，在对应阶段定义。阶段 1 的 `scan` 只会给出 `WAIT` 或 `DATA_ERROR`。

## 策略（阶段 2）

- **`StrategyProvider`**：所有策略共用的插件接口。策略是无副作用的纯函数。见 `docs/strategy-spec.md`。
- **`OrderIntent`**：标准订单意图。订单类型 `STOP_LIMIT_NEXT_BAR` | `MARKET_NEXT_OPEN` | `LIMIT_RETRACE`。
- **目标描述（`TargetSpec`）**：`ABSOLUTE_PRICE_TICKS`（绝对价）、`OFFSET_FROM_FILL_TICKS`（相对成交价的 tick 距离）、`INITIAL_RISK_MULTIPLE`（初始风险的倍数，即几 R）。
- **`setup_id`**：一个信号的稳定标识，由策略家族、实际合约、形态、信号 K 时刻和版本生成。
- **强趋势 K**：实体 ≥ 全幅的 50%、收在方向一侧的 1/3 之内、创出新的方向极值的 K 线。
- **Spike（尖峰）**：至少 2 根连续同向的强趋势 K，首根开盘到末根收盘的幅度 ≥ 2.5 × ATR。
- **回调**：Spike 之后第一根不再创新极值的 K 起算。回撤比例 = 反向走过的最大距离 ÷（Spike 极值 − Spike 首根开盘）。
- **反向腿**：回调中某根严格越过前一根的反向极值（牛 Spike 的回调里是破前一根低点），开启一条腿。
- **H1／H2／H3、L1／L2／L3**：牛 Spike 回调中每条反向腿之后第一根合格的顺势信号 K，依次标 H1–H3；熊 Spike 镜像，标 L1–L3。
- **信号 K**：触发订单意图的那根 K。下一根 K 是它的有效期。
- **周期（`cycle`）**：`BULL_SPIKE`、`BEAR_SPIKE`、`BULL_MICROCHANNEL`、`BEAR_MICROCHANNEL` 或 `UNKNOWN`。
- **`skip_reason`**：信号被拦截或无法判定的原因，如 `TAIL_WINDOW_UNCONFIGURED`、`TWO_STOPS_HALT`、`FEATURE_UNAVAILABLE:atr14`。
- **定点精度**：EMA 和 ATR 以 1e-9 tick 为单位的整数保存。

## 引擎（阶段 3）

- **决策时点**：引擎处理一根信号 K 的时刻，也是挂单的激活时刻。回放里取信号 K 的可用时点。
- **过期时刻**：信号 K 收盘 + 有效根数 × 周期，且不晚于截止强平时刻。
- **子 K**：喂给成交模型的行情。有 1 分钟线时是 1 分钟线，否则是 5 分钟线本身。
- **订单状态**：`ARMED`、`TRIGGERED`、`FILLED`、`EXPIRED`、`CANCELLED`。
- **退出原因**：`STOP`、`TARGET`、`FORCED_FLAT`、`REVERSAL`。
- **截止强平**：RTH 收盘前 5 分钟（常规日 15:55 ET）撤掉未成交的入场单并平掉持仓。
- **歧义标记**：`AMBIGUOUS_ENTRY`（入场跨着激活时刻）、`AMBIGUOUS_BAR`（止损与目标先后不明，按止损）、`AMBIGUOUS_PATH`（一次目标触及没有被采信）。完整列表见 `docs/engine.md`。
- **运行（run）**：引擎的一次连续执行，有自己的账本、策略状态和水位。`run_id` 加运行模式构成它的风险域。
- **风险域**：纸面以 scope、运行、交易日为键；真实账户以账户、交易日为键。两者互不影响。
- **每手计划风险**：止损 tick 数 × tick 价值 + 往返费用 + 滑点预算。
- **R**：`r_initial` 按该笔的初始止损距离归一化；`r_research` 按预登记的固定研究风险单位。
- **引擎版本**：成交规则和事件顺序的版本号，现在是 `fill-v1.0`。
- **`FROZEN_CONTRACT`**：阶段 1–3 验收后冻结的表结构、状态枚举和引擎公共接口。之后的改动要走编号 migration 并重跑全部既有测试。

## 人工轨（阶段 4）

- **计划（plan）**：下单之前登记的交易打算。状态 `DRAFT`、`ACTIVE`、`PARTIALLY_FILLED`、`FILLED`、`CANCELLED`、`EXPIRED`。激活才预留风险预算。
- **预检结论**：`ALIGNED`、`DEVIATES`、`BLOCKED_BY_RISK`。无法确认与策略一致时给 `DEVIATES`。
- **`strategy_alignment`**：计划（或交易）与当时的正式策略是否一致：`ALIGNED`、`DEVIATES`、`UNKNOWN`。
- **`plan_adherence`**：真实动作与当时生效的个人计划是否一致：`COMPLIANT`、`VIOLATION`、`UNKNOWN`。与上一条独立。
- **`plan_link_status`**：开仓成交与事前计划的关联：`LINKED`（可证明）、`UNKNOWN`（只有时间吻合，待核对）、`UNPLANNED`。
- **隔离（quarantine）**：没有成交号且无法唯一去重的成交，不参与任何统计。
- **纪律事件**：`UNPLANNED`、`COUNTERTREND_BEFORE_CONFIRMATION`、`RETRY_WITHOUT_NEW_SETUP`、`EARLY_EXIT`、`STOP_WIDENED`、`STOP_DELAYED`、`MISSING_EVIDENCE`。
- **纪律提醒**：`watch` 的六条规则：`NAKED_POSITION`、`STOP_MOVED_AWAY`、`REENTRY_IN_COOLDOWN`、`UNPLANNED_ENTRY`、`COUNTERTREND_ADD`、`DAILY_LOSS_NEAR_LIMIT`。
- **保护止损单**：与持仓同合约、方向相反、止损类型、数量不小于持仓的挂单。
- **`plan_coverage`**：有事前已激活计划关联的开仓数 ÷ 全部开仓数。人工轨的第一指标。
- **账户快照**：只读适配器取回的持仓、挂单和当日成交，带取数时刻。
- **常驻纸面运行**：每个 scope 一个，`run_id` 为 `paper:<scope>`，盘中 `scan` 用它。

## 回放与基线（阶段 5）

- **开发可见截止日（`dev_visible_through`）**：开发数据和保留数据的分界，现为 2024-12-31。晚于它的 K 线不进运行库，只记覆盖和内容摘要。
- **内容摘要**：逐根 K 线的 sha256 当作 256 位整数相加取模，与顺序无关。不保存价格也能核对内容。
- **有效交易日**：当日实际合约的 RTH 数据完整且来源质量未降级。零交易日也是有效日。
- **切分段**：`DEVELOPMENT`（开发）、`FORWARD_VALIDATION`（向前验证）、`CAPSULE`（胶囊）、`REMAINDER`（不足一个胶囊的余量）。
- **胶囊（capsule）**：截止日之后连续的 20 个有效交易日，状态 `SEALED`。默认 1 个盲筛、紧接的 2 个合成 40 日确认。
- **预热日**：数据起点后的前 20 个交易日，回放照跑但不计入度量。
- **基线（baseline）**：一个 scope 冻结的比较基准。`baseline_id` 由冻结清单的摘要得出。状态 `PENDING`、`FROZEN`、`NON_DETERMINISTIC`。
- **冻结清单**：策略配置、指标定义、引擎与风控版本、费用、时段、行情清单（含 revision）、切分登记和代码摘要。
- **研究运行**：`BASE`（主运行，保留明细）、`VERIFY`（校验运行，只留摘要）、`VARIANT`（扰动，只在开发段）。
- **扰动（perturbation）**：预登记的配置差异，只用来估计配对日差和规划功效。不是候选，不是盈利证据。
- **可改键白名单（`EVOLVABLE_KEYS`）**：策略登记的、允许由配置差异改动的键。
- **Track A／Track B**：`PRUNING_DEFENSIVE`（剪枝防守，检验非劣效加回撤改善）和 `ALPHA_EXPANSION`（结构扩展，检验净提升）。
- **配对日差**：同一交易日上候选净 R 减基线净 R。零交易日计入。
- **短窗正式资格**：40 日确认在规划效应下的整体通过率是否达到功效目标。`ELIGIBLE` 或 `EVIDENCE_INSUFFICIENT`。
- **`FIXTURE_ONLY`**：报告只用了合成数据。策略有效性为 `EVIDENCE_INSUFFICIENT`。
- **滑点压力**：每边再多 1 tick 滑点后的净结果。
- **风险规模结果**：对固定 1 手的交易按账户预算重新定手数后的结果。账户限额未批准时不出数。

## 数据位置

- **数据根目录（`data_root`）**：项目之外，默认 `~/trading-agent-data/`。存放原始行情（`databento/`、`webull/`）和运行库（`runtime/`）。
- **运行库**：`<data_root>/runtime/trading_agent.sqlite`，全部业务表。
- **发布库**：项目内 `releases/release-state.sqlite`，只有 active 指针和审计事件，只有发布入口可写。
- **保留集**：项目之外的独立路径，只在 `config/paths.yaml` 登记。

## 快照

一次扫描所用数据的封存记录，带唯一 ID 和内容摘要。相同输入得到相同摘要。

## 数据异常类别

见 `docs/data-contract.md` 的「校验规则与异常类别」。
