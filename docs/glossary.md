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

- **股票类品种**：没有合约月、不换月的品种（换月规则种类 `none`），如 SPY、QQQ。合约标识就是代码，tick 是 0.01 美元，数量是股数。只进人工轨，没有策略 scope 和基线。
- **账户设置（`config/accounts.yaml`）**：每个账户自己的限额、费用、滑点预算和券商。不影响纸面评测的 `risk_version`。

## 回放与基线（阶段 5）

- **开发可见截止日（`dev_visible_through`）**：开发数据和保留数据的分界，现为 2024-12-31。晚于它的 K 线不进运行库，只记覆盖和内容摘要。
- **实时行情放行（`config/live_data.yaml`）**：登记来源从登记首日起的 K 线，晚于截止日也进运行库，只供纸面运行、影子和监控；切分登记把这些日子排除在所有段之外。
- **内容摘要**：逐根 K 线的 sha256 当作 256 位整数相加取模，与顺序无关。不保存价格也能核对内容。
- **有效交易日**：当日实际合约的 RTH 数据完整且来源质量未降级。零交易日也是有效日。
- **切分段**：`DEVELOPMENT`（开发）、`FORWARD_VALIDATION`（向前验证）、`CAPSULE`（胶囊）、`REMAINDER`（不足一个胶囊的余量）。
- **胶囊（capsule）**：截止日之后连续的 20 个有效交易日，状态 `SEALED`。现行协议：1 个盲筛，紧接的 6 个合成 120 日确认。
- **预热日**：数据起点后的前 20 个交易日，回放照跑但不计入度量。
- **基线（baseline）**：一个 scope 冻结的比较基准。`baseline_id` 由冻结清单的摘要得出。状态 `PENDING`、`FROZEN`、`NON_DETERMINISTIC`。
- **冻结清单**：策略配置、指标定义、引擎与风控版本、费用、时段、行情清单（含 revision）、切分登记和代码摘要。
- **研究运行**：`BASE`（主运行，保留明细）、`VERIFY`（校验运行，只留摘要）、`VARIANT`（扰动，只在开发段）。
- **扰动（perturbation）**：预登记的配置差异，只用来估计配对日差和规划功效。不是候选，不是盈利证据。
- **可改键白名单（`EVOLVABLE_KEYS`）**：策略登记的、允许由配置差异改动的键。
- **Track A／Track B**：`PRUNING_DEFENSIVE`（剪枝防守，检验非劣效加回撤改善）和 `ALPHA_EXPANSION`（结构扩展，检验净提升）。
- **配对日差**：同一交易日上候选净 R 减基线净 R。零交易日计入。
- **短窗正式资格**：确认窗（现为 120 日）在规划效应下的整体通过率是否达到功效目标。`ELIGIBLE` 或 `EVIDENCE_INSUFFICIENT`。
- **`FIXTURE_ONLY`**：报告只用了合成数据。策略有效性为 `EVIDENCE_INSUFFICIENT`。
- **滑点压力**：每边再多 1 tick 滑点后的净结果。
- **风险规模结果**：对固定 1 手的交易按账户预算重新定手数后的结果。账户限额未批准时不出数。

## 日复盘（阶段 6A）

- **session**：一个运行的一个交易日（`<run_id>:<交易日>`），或一个账户一个交易日的人工轨（`manual:<账户>:<交易日>`）。
- **封存（`SEALED`）**：把 session 的当日指标固定下来。只有封存的 session 才能复盘。
- **四类信息**：当时知道的事实、证据不足的判断、事后信息、改动假设。复盘里分开写。
- **归因类别**：`DATA_ENGINE_ERROR`、`RULE_EXECUTION_DEVIATION`、`LOSS_BY_RULE`、`PROFIT_BY_RULE`、`OUT_OF_RULE_OBSERVATION`。盈利的违规仍是违规。
- **观察（observation）**：带 scope、机制标签和证据的一条记录。只描述发生了什么。
- **机制标签**：观察的固定分类，如 `EXPIRED_THEN_TRIGGERED`、`STOPPED_THEN_TARGET`、`TWO_STOPS_HALT`。完整列表见 `docs/daily-review.md`。
- **事故草稿**：`DATA_ERROR`、引擎异常、重大成交歧义的立案，含检查点和原始事件。状态 `DRAFT`，确认后才进回归库。
- **检查点**：事故前某一刻的策略状态、版本元组、水位、仓位和挂单，用于以后重放。
- **Memory 候选**：由明确表达的偏好生成，状态 `PENDING_CONFIRMATION`。

## 聚合演化（阶段 6B）

- **聚类（cluster）**：窗口内同一 scope、机制、原因、配置差异键的观察合在一起。状态 `OBSERVATION` 或 `PROMOTABLE`。
- **配置差异键（`diff_key`）**：一个机制对应的、策略可改键白名单里的键。没有对应键的聚类只能停在观察。
- **暴露条件**：一个机制有机会出现的前提（如"当日有到期订单"）。满足它却没出现该机制的 session 是**反例**。
- **升级规则**：出现在至少 `min_days`（3）个不同交易日，才能支撑 `PROPOSED` 候选。
- **提案**：候选的来源。预登记的模板（`config/evolve.yaml`）或导入的文件，走同一套校验。
- **候选（candidate）**：只改一个白名单键的结构化差异，带证据、假设、门槛和回滚条件。状态 `OBSERVATION`、`PROPOSED`、`SUPERSEDED`；评测之后的状态在阶段 8 定义。
- **候选类别**：`strategy_rule`、`explanation_discipline`、`data_engine`、`memory`。
- **`base_release`**：候选所基于的正式版本。还没有发布时写 `baseline:<baseline_id>`。
- **候选台账（`candidate_ledger`）**：候选的全部事件，只追加。
- **假设家族**：α 记账的单位。策略类候选取验证轨道。
- **开发尝试**：对一个候选做了一次效应估计。
- **效应估计**：候选在开发段加向前验证段上相对基线的平均配对日差。只用于排队。
- **排序分**：效应项 × 证据天数权重 × 机制可信度，公式预登记。
- **`SUPERSEDED`**：排队超过 `queue_ttl_days`，或基准被替换。
- **队列前向预演（Pre-Eval Shadow）**：只查可运行性的回放，不报告盈亏；看过的日期不得进入正式确认窗。
- **候选组**：同家族、同机制、同差异键出现在多个品种时的分组，各 scope 分别评测。
- **`NO_CANDIDATE`**：这次运行没有生成或刷新任何候选。

## 数据位置

- **数据根目录（`data_root`）**：项目之外，默认 `~/trading-agent-data/`。存放原始行情（`databento/`、`webull/`）和运行库（`runtime/`）。
- **运行库**：`<data_root>/runtime/trading_agent.sqlite`，全部业务表。
- **发布库**：项目内 `releases/release-state.sqlite`，只有 active 指针和审计事件，只有发布入口可写。
- **保留集**：项目之外的独立路径，只在 `config/paths.yaml` 登记。

## 快照

一次扫描所用数据的封存记录，带唯一 ID 和内容摘要。相同输入得到相同摘要。

## 数据异常类别

见 `docs/data-contract.md` 的「校验规则与异常类别」。

## Memory（阶段 7）

- **Memory 条目**：一条已确认的长期记忆，只由结构化字段组成。种类 `user_preference`、`market_fact`、`discipline_pattern`。
- **`statement`**：条目的内容：`subject`、`condition`、`metric`、`value`、`unit`、`sample_n`、`period`、`source`。每个字符串是枚举或固定文法。
- **登记表（`config/memory.yaml`）**：偏好键、指标、条件键、来源和任务相关性的白名单。
- **禁止项**：风险上限和实时状态的名字，写在代码里，登记表放行不了。
- **`memory_version`**：一组 `(item_id, version)` 的快照，标识由成员列表的摘要得出。不可变。
- **头指针（`memory_head`）**：当前生效的 `memory_version`。回滚只移动它。
- **`scope_digest`**：某个 scope 实际加载到的条目的摘要。
- **`PENDING_EVALUATION`**：市场事实或执行模式已经你确认，等阶段 8 的评测通过后才生效。
- **模板句子**：由条目字段和登记表里的标签生成、带 `[item_id]` 的一句话，是模型看到的唯一形式。

## 独立评测（阶段 8）

- **预登记**：窗口解封之前冻结候选摘要、基线、协议和两段窗口。之后不可修改。
- **协议（`protocol_id`）**：阈值、成本、主指标、α、bootstrap 参数、可见字段和窗口的正文摘要。
- **盲筛（阶段 A）**：1 个胶囊上的事件级 A/B 回放，只淘汰。结果 `SCREEN_CONTINUE`、`SCREEN_REJECTED`、`EVIDENCE_INSUFFICIENT`。
- **正式确认（阶段 B）**：独立确认窗上的一次正式检验。结果 `PASS_FOR_SHADOW`、`FAIL`、`EVIDENCE_INSUFFICIENT`。
- **评测方数据库**：评测进程在 `holdout_root` 下自己的数据库，含截止日之后的行情和两臂的逐笔。
- **解封账本（`window_ledger`）**：胶囊的 `ASSIGNED`、`UNSEALED`、`RETURNED`，按品种和实际日期记录，只追加。
- **置信下界**：配对日差均值的单侧界，由连续交易日区块 bootstrap 得出。
- **差异交易检验**：基线入场但候选拦截的交易的平均净 R 及其单侧上界。只作辅助。
- **滑点压力门槛**：每边多 1 tick 滑点后，候选净结果为正且不比基线差。
- **标注类评测**：解释／纪律类和 Memory 类用人工标注的识别率、误报率、引用正确率评测。
- **事故回归库**：经确认、能独立重放的事故条目。状态 `CONFIRMED` 或 `INCOMPLETE`。
- **合成回归**：评测前运行的一组固定测试文件。
- **摘要清单**：`holdout_root/protected/evaluator-manifest.json`，评测器和引擎代码的摘要，由评测身份写入。
- **隔离验收**：以作者身份尝试四条路径读保留集，都被挡住才算通过。

## 影子、发布与监控（阶段 9）

- **正式影子（`SHADOW`）**：通过正式确认的候选与当前 active 版本读同一批后续行情、各写各的账本的运行验收。计划在登记时冻结，到终点才判定：`PASSED`、`REJECTED`、`EVIDENCE_INSUFFICIENT`。
- **影子计划**：起始日、有效日数、每臂最低成交、检查项、窗口性质、可选的非劣效设置。登记后不可修改，没有延长窗口的入口。
- **窗口性质**：`OPERATIONAL_ACCEPTANCE`（运行验收）或 `FORWARD_HOLDOUT`（前向保留：结束前不进作者的复盘和聚合输入）。
- **实时前向预演**：队列前几名挂实时行情的 `pre_eval_shadow` 运行，只记可运行性，不报告盈亏。
- **发布清单（`release_manifest`）**：一个版本的全部依据：版本元组与摘要、配置差异、评测与影子结果、回滚目标、触发条件、改动说明与代价。`manifest_hash` 是它的规范化 JSON 的 sha256。
- **`config_diff`**：一个版本相对正式配置文件的全部差异。盘中策略 = 正式配置 + `config_diff`。
- **首次纸面基线（`INITIAL_PAPER_BASELINE`）**：scope 第一次建立 active 时的版本，绑定冻结基线，标 `UNVALIDATED_BASELINE`，要过五项验收。只建档，不代表策略有效。
- **active 指针**：发布库里每个四维 scope 一条，指向当前版本，带入场权限 `entries_enabled`。只由批准和回滚改写。
- **人工确认**：批准人名单里的系统用户在交互终端里手输摘要前缀。不是安全边界。
- **发布守卫**：发布服务代码的摘要须与受保护摘要清单的 `release_sha256` 一致。
- **冻结原因**：`DATA`、`DAILY_LOSS`、`MANUAL`、`MONITOR`、`RELEASE`、`CONTEXT`（阶段 10），分开记录、按各自的政策解除。
- **回滚**：active 指回上一已批准版本；没有上一版本时冻结并停用入场权限。
- **回滚触发条件（`rollback_trigger`）**：监控用的阈值，准备清单时算好并冻结进清单。
- **预期区间**：触发条件的来源。首次纸面基线取阶段 5 开发段的基线主运行。

## 调度、告警与控制通道（阶段 10）

- **节拍（tick）**：盘中每 5 分钟一次，有新的已收线、已可用的 5 分钟线才扫描各 scope 并推进影子。
- **水位（watermark）**：每个 scope 已处理的最后一根 5 分钟线的收线时刻，存在 `ops_state`，用来去重。
- **运行锁**：`ops_locks` 里带有效期的锁，防止同一动作重复运行；过期可接管。
- **心跳**：各动作（`tick`、`daily`、`weekly`）最后一次成功的时刻；watch 另有自己的心跳。
- **去重键与静默窗口**：同一去重键的告警在静默窗口内只推送一次，其余记 `SUPPRESSED`。
- **`BACKLOG`**：第一次分发之前积压的告警，不推送。
- **控制通道**：经适配器校验来源、发送者已绑定的白名单命令。每条消息写进只追加的 `control_commands`。
- **确认令牌**：`unfreeze` 的第二步，绑定原请求、通道和发送者，短期有效，只能用一次。
- **当日约束（context）**：`no-entry` 或 `max-qty`，只能收紧风险；禁止入场以冻结原因 `CONTEXT` 生效，到期自动解除。
- **死信队列**：连续失败达到上限的解释请求；`ops --dlq-replay` 放回队列。

