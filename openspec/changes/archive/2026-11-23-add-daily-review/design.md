## Context

运行库里已有复盘需要的全部原料：`scan_events`（逐条决策、策略状态、版本元组）、`paper_orders`／`paper_trades`、`trader_plans`、`manual_trades`、`manual_order_events`、`discipline_events`、`discipline_alerts`、`trader_notes`、`data_anomalies`、`alerts`。`sessions` 表从阶段 1 就在，阶段 4 往里写了人工轨的指标（`manual:<账户>:<交易日>`，状态 `OPEN`），纸面运行还没有 session。`SessionReview`、`MemoryCandidate`、`Candidate` 三个 schema 已冻结。项目还没有任何模型调用，只有一个解释请求队列。

基线主运行覆盖 1340 个交易日，可以直接拿来验收复盘。

## Goals / Non-Goals

**Goals**
- 可封存、可复盘、可重现：同一个 session 两次复盘结果相同。
- 结论由规则得出，每条都带 `event_id`。
- 事后信息与当时的事实严格分开。

**Non-Goals**
- 不调用模型。不做跨日聚合和候选升级（6B）。不实现 Memory 存储与加载（7）。不把事故草稿放进回归库（8）。
- 不处理晚于开发可见截止日的真实数据。

## Decisions

**1. 复盘由 Python 确定性生成，不调用模型。** `CLAUDE.md` 规定 Python 决定信号、风控、成交和盈亏，Claude 解释和引用证据。归因、计数、分组都是可以由规则判定的事；交给模型反而不可重现。模型以后的角色是在这份结构化结果之上写叙述：复盘时把一份压缩上下文（不含原始 K 线）放进已有的 `explanation_requests` 队列，类型 `SessionReview`，本阶段没有消费者。没有选"先接模型"：没有调用通道，且会把二期的进度绑在模型接入上。

**2. session 的标识与封存。** 纸面 session 的标识是 `<run_id>:<交易日>`。封存写 `sessions`（状态 `SEALED`、`sealed_at_utc`、`kpi_json`）。指标包含：决策数、机会数、订单数、成交数、净盈亏、净 R、标记计数、数据状态、当日版本元组。封存后不改：重复封存返回原记录。人工轨的 session（`manual:…`）沿用阶段 4 的记录，复盘时按账户和交易日带上，不另行封存——它的数据来自券商导入，可能迟到，复盘里注明导入时刻。

**3. 模块划分**（`src/trading_agent/review/`）：
   - `seal.py`：封存条件与指标。
   - `facts.py`：从账本读出一个 session 的全部事实（决策、订单、交易、异常、人工轨）。
   - `rules.py`：归因、观察机制、事后信息的规则。纯函数，输入是 `facts` 的结果。
   - `incidents.py`：事故草稿与检查点。
   - `build.py`：组装 `SessionReview`，校验，落库落盘。
   - `render.py`：Markdown。
   度量复用 `research/metrics.py`（MAE／MFE、分组），不重写。

**4. 归因规则。** 每个对象只归一类，按下面的顺序取第一个命中的：
   1. 数据或引擎异常（`DATA_ERROR` 的决策、`UNPRICEABLE`、`INVALID_AFTER_FILL`、当日的引擎告警）→ `DATA_ENGINE_ERROR`。
   2. 人工平仓 `plan_adherence=VIOLATION`，或开仓 `UNPLANNED`，或带纪律事件 → `RULE_EXECUTION_DEVIATION`（无论盈亏）。
   3. 人工平仓 `plan_adherence=UNKNOWN` → `OUT_OF_RULE_OBSERVATION`（证据不足，同时进"证据不足的判断"）。
   4. 其余按净盈亏：亏或平 → `LOSS_BY_RULE`，盈 → `PROFIT_BY_RULE`。纸面交易由引擎按规则执行，不存在执行偏差，只会落在 1 或 4。
   被拦截的机会不归因（它们不是结果），列在"机会"里。

**5. 观察的机制标签是固定的一组**，各有触发规则和证据：
   - `TWO_STOPS_HALT`：当日触发了两次止损停手。
   - `EXPIRED_THEN_TRIGGERED`：挂单到期后同一交易日内价格越过触发价（事后信息）。
   - `STOPPED_THEN_TARGET`：止损出场后同一交易日内价格到过原目标（事后信息）。
   - `FILTERED_SETUP`：信号被过滤器拦截（带过滤原因）。
   - `AMBIGUOUS_FILL`：成交带歧义标记。
   - `FORCED_FLAT`：持仓到截止强平。
   - `DATA_QUALITY`、`ENGINE_ANOMALY`：数据或引擎问题（这两类产出候选）。
   - 人工轨：每种纪律事件类型一个标签，`WATCH_ALERT_IGNORED`（提醒后未处理或再犯）。
   标签只描述"发生了什么"，不下"应该怎么改"的结论；改动假设留给 6B。"改动假设"一类在 6A 里只出现在数据／引擎候选上。
   观察的自然语言说明由模板生成。标签集合放在代码里而不是配置里：它们与归因规则是一体的，改了要有测试。

**6. 事后信息用开发可见的行情计算**，时间范围限于该 session 的交易日之内（到当日 RTH 收盘），不看次日。这样复盘不依赖之后的数据，重放时结果不变。

**7. 数据／引擎候选。** 直接写 `candidates`（状态 `PROPOSED`，类别 `data_engine`，`eval_track=CHEAP_NON_STRATEGY`）和 `candidate_ledger`。候选标识由（scope、机制、证据的事故草稿标识）的摘要得出，重复复盘不重复登记。`exact_diff` 留空对象并注明"走引擎修复流程"：6A 不知道怎么修，只立案。候选对象通过 `Candidate` schema；schema 要求而 6A 给不出的字段用明确的占位值（如 `evaluation_protocol_id: "ENGINE_FIX_FLOW"`）。若 schema 不允许这样的占位，则只登记台账记录、不写候选对象，并在文档里说明。

**8. 事故草稿的检查点。** `scan_events` 每条都带当时的 `strategy_state_json` 和版本元组，所以检查点 = 事故前 48 小时内最早的一条决策记录的状态，加上那一刻的仓位和挂单（从账本按时间重建）。草稿存 `incident_drafts` 表，正文同时写到 `<data_root>/incidents/drafts/<incident_id>.json`（项目之外，因为含行情 revision 清单）。行情只存键和 revision（来源、合约、周期、起始时刻、revision），不存价格副本：运行库本身保留全部 revision。缺口从 `data_anomalies` 和预期格子的差集得出。能否独立重放由阶段 8 验证；6A 的草稿状态是 `DRAFT`。

**9. 对备注的回应。** 规则只能核对两种备注：
   - 以 `偏好:` 开头的键值 → 回应 `ACCEPT`，并生成 Memory 候选（键在白名单内）或 `REBUT`（键不在白名单，证据是白名单本身）。
   - 关联了 `event_id` 且以 `更正:` 开头、内容为 `plan_link=<plan_id>` 的 → 核对该计划是否存在且事前激活：是则 `ACCEPT`（只登记，不改账本），否则 `REBUT`。
   其余一律 `PENDING`。Python 不读懂自由文本，这是如实的做法。
   白名单先放两个键：`report_max_lines`、`watch_poll_interval_seconds`（提示词包阶段 7 的例子）。

**10. 复盘的存储与幂等。** `session_reviews` 以 `session_id` 为主键，保存 `review_json`、摘要和所用代码版本。同一 session 重新复盘会覆盖并把 `version` 加一（规则改进后重跑是正常的），旧版本的摘要进 `superseded_json`。观察按（session、机制、证据）去重。

**11. 批量。** `review --from --to` 逐日封存并复盘。1340 天的基线主运行全部复盘预计几分钟；报告文件按 session 一个，放 `reports/sessions/`（不进版本控制）。

## Risks / Trade-offs

- 规则生成的复盘比较干，不会"读懂"一天的走势 → 这是有意的；叙述留给以后的模型层，结论先保证可重现。
- 机制标签是我定的固定集合，可能漏掉值得看的现象 → 列在文档里，等用户增删；6B 只聚合已有标签。
- 事后信息只看当日 → 隔夜之后才显现的问题看不到；换来的是不依赖未来数据。
- 人工轨目前没有真实成交（账户是 Alpaca 模拟盘，不能交易 ES）→ 人工轨部分只能用合成数据验收。

## Migration Plan

应用 migration 0005。不改既有数据。回退：删掉新表即可，其他表不受影响。

## Open Questions

- 机制标签的集合和备注的两种可核对格式，等用户确认。
