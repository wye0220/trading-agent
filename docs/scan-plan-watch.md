# scan、plan、watch 与人工成交导入（阶段 4）

术语见 `docs/glossary.md`。这一阶段给了三个能用的命令和一个导入入口。它们都是只读的：不向任何券商提交、修改或撤销订单。

| 命令 | 作用 |
| --- | --- |
| `scan` | 数据校验后运行策略和纸面账本，输出 JSON 和中文短报 |
| `plan` | 登记一份人工交易计划并预检；显式激活才预留预算 |
| `watch` | 按间隔读取真实账户的快照，发现违反纪律的情形时提醒 |
| `ingest --trades … --order-events …` | 导入人工成交和订单事件，生成纪律事件和每日指标 |

**现状**：没有实时行情来源，也没有真实券商接口。所以盘中 `scan` 现在会报 `DATA_ERROR`（没有数据），`watch` 只能读你或别的工具写的本地快照文件。

---

## 一、scan

```bash
uv run python -m trading_agent scan                                   # 盘中
uv run python -m trading_agent scan --as-of 2025-11-25T15:30:05Z      # 历史
uv run python -m trading_agent scan --json
```

### 盘中

每个 scope 有一个常驻的纸面运行，`run_id` 为 `paper:<scope>`。每次扫描：

1. 校验行情的来源、新鲜度和完整性（规则见 `docs/data-contract.md`）。
2. 把校验结论写到这个运行的风险域上。有异常时结果是 `DATA_ERROR`：引擎照常推进已有的纸面仓位，但不会挂新单。
3. 调用引擎的一步（`docs/engine.md` 第三部分）。

对同一根信号 K 重复扫描不产生新的订单或扫描记录。

### 历史

策略要从当日开盘逐根推进才有状态，所以带 `--as-of` 的扫描开一个一次性的运行（`run_id` 以 `hscan_` 开头），把当日回放到那个时点。

**真实数据的保护**：该交易日是真实数据时，只有同时满足"带 `--allow-real-data`、已设 `dev_visible_through`、该交易日不晚于它"才运行策略。否则只返回数据校验结果，并在 `strategy_note` 里说明原因。合成数据不受限制。

### 输出

JSON 的主要字段：`signal_status`、`risk_status`、`paper_action`、`block_reason`、`order_id`、`contract_id`、`session`、`snapshot_id`、`latest_bar`（含完成时刻和可用时点）、`version_tuple`、`order_intent`、`decision_metadata`、`strategy_ran`、`paper_position`、`working_order`、`problems`、`news_windows`、`report`。

- `WAIT` 时 `decision_metadata.next_confirmation` 给出下一确认点，`skip_reason` 给出过滤或等待的原因。
- 风控拦截时 `risk_status = BLOCKED`，`block_reason` 给出原因（如 `FROZEN:MANUAL`、`DATA_BLOCKED:…`、`AFTER_CUTOFF`）。

中文短报：首行是纽约时间、scope、实际合约、状态、版本，之后是事实、决策、持仓、纪律、数据五行，最后是免责声明。策略结构那一段是把决策元数据 `structure` 里的标量字段原样列出，扫描器不解释任何策略专有的含义。

### 资讯窗口

`config/news_windows.yaml` 里列出带明确起止时刻的条目。没有条目时短报写"资讯窗口：不可用（未配置）"。有条目时列出正在进行的和 4 小时内的。扫描不会推测资讯时间，也不会因为资讯窗口自动拦截入场（那是交易员通过控制通道给的当日约束，阶段 10）。

### 样例

下面三段由测试生成，并与实际输出逐字比对（`tests/test_scanner.py::test_doc_samples_match_actual_output`）。数据是合成的。

等待：

```text
[2025-11-25 10:20:05 ET] [pa_brooks:ES:RTH:5m] [ESZ2025] [WAIT] [pa_brooks@v1.0 / fill-v1.0] [FIXTURE]
事实：最新信号 K 10:15:00–10:20:00 ET，开 5011.50 高 5011.75 低 5009.00 收 5009.50。策略结构：cycle=BULL_SPIKE，htf_direction=UNKNOWN，stops_today=0，halted=False。PDH 不可用 / PDL 不可用 / 上一 RTH 收盘 不可用，ONH 5002.00 / ONL 4999.00，ADR 有效日 0/10。
决策：WAIT。没有新信号。下一确认点：等待新的反向腿之后出现合格的顺势信号 K。
持仓：无纸面仓位，无挂单。
纪律：今日纸面止损 0 次；允许在出现新的 setup 时再尝试。
数据：快照 snap_5e8a959336ec1ba26df09dbc，来源 fixsrc，最新 bar 完成 10:20:00 ET、到达 10:20:05 ET（估计）。资讯窗口：不可用（未配置）。
以上为规则化的价格行为学习标注，不构成投资建议。期货有杠杆，风险自负。
```

纸面入场：

```text
[2025-11-25 10:30:05 ET] [pa_brooks:ES:RTH:5m] [ESZ2025] [SETUP_VALID] [pa_brooks@v1.0 / fill-v1.0] [FIXTURE]
事实：最新信号 K 10:25:00–10:30:00 ET，开 5007.50 高 5010.00 低 5007.25 收 5009.75。策略结构：cycle=BULL_SPIKE，htf_direction=UNKNOWN，stops_today=0，halted=False。PDH 不可用 / PDL 不可用 / 上一 RTH 收盘 不可用，ONH 5002.00 / ONL 4999.00，ADR 有效日 0/10。
决策：新信号 spike_pullback H1，做多。纸面挂单 STOP_LIMIT_NEXT_BAR：触发 5010.25，限价 5010.50，止损 5006.75，目标 INITIAL_RISK_MULTIPLE=2；有效至 10:35 ET。
持仓：无。挂单 ARMED：触发 5010.25，到 10:35 ET 过期。
纪律：今日纸面止损 0 次；允许在出现新的 setup 时再尝试。
数据：快照 snap_ede0e9954ec9370a6b44d049，来源 fixsrc，最新 bar 完成 10:30:00 ET、到达 10:30:05 ET（估计）。资讯窗口：不可用（未配置）。
以上为规则化的价格行为学习标注，不构成投资建议。期货有杠杆，风险自负。
```

被风控拦截（该 scope 被人工冻结）：

```text
[2025-11-25 10:30:05 ET] [pa_brooks:ES:RTH:5m] [ESZ2025] [SETUP_VALID·BLOCKED] [pa_brooks@v1.0 / fill-v1.0] [FIXTURE]
事实：最新信号 K 10:25:00–10:30:00 ET，开 5007.50 高 5010.00 低 5007.25 收 5009.75。策略结构：cycle=BULL_SPIKE，htf_direction=UNKNOWN，stops_today=0，halted=False。PDH 不可用 / PDL 不可用 / 上一 RTH 收盘 不可用，ONH 5002.00 / ONL 4999.00，ADR 有效日 0/10。
决策：有信号 spike_pullback H1，但没有挂单。原因：FROZEN:MANUAL。
持仓：无纸面仓位，无挂单。
纪律：今日纸面止损 0 次；允许在出现新的 setup 时再尝试。
数据：快照 snap_ede0e9954ec9370a6b44d049，来源 fixsrc，最新 bar 完成 10:30:00 ET、到达 10:30:05 ET（估计）。资讯窗口：不可用（未配置）。
以上为规则化的价格行为学习标注，不构成投资建议。期货有杠杆，风险自负。
```

---

## 二、plan

```bash
uv run python -m trading_agent plan ES L 5000 4995 5010 1 PA Spike H1 --account U123
uv run python -m trading_agent plan --activate plan_xxxxxxxxxxxx
uv run python -m trading_agent plan --cancel plan_xxxxxxxxxxxx
uv run python -m trading_agent plan --list
```

### 快捷语法

`根代码 方向 入场 止损 目标 手数 理由…`

- 方向：`L` 或 `S`。
- 止损、目标可以写 `-` 表示没有。
- 价格必须在最小变动价位上；止损和目标必须在入场的正确一侧。
- 根代码解析为当日的实际合约，输出里回显。
- 账户：`--account`，或 `config/manual.yaml` 的 `default_account_id`。
- 失效时间：提交后 `plan_ttl_minutes`（默认 30）分钟。

### 状态

`DRAFT → ACTIVE → PARTIALLY_FILLED → FILLED`，另有 `CANCELLED`、`EXPIRED`。

- 新计划是 `DRAFT`，**不预留预算**。
- `--activate` 重新预检；风险通过才在同一事务里预留风险并转为 `ACTIVE`。风险不通过时保持 `DRAFT`，退出码非零。
- 过了失效时间仍未成交的 `DRAFT`、`ACTIVE` 转为 `EXPIRED`；取消转为 `CANCELLED`。两者都释放预留。
- 成交导入把关联的开仓计入计划；满额时转 `FILLED` 并释放预留。
- 每次状态变化写一条 `plan_events`，更新带 `version` 校验。

### 预检

三部分各自给出：

| 部分 | 怎么来的 |
| --- | --- |
| `strategy_alignment` | 用该 scope 纸面运行的最新策略状态调用策略的一致性评估。没有运行，或状态落后超过 `strategy_state_max_age_bars`（默认 2）根 K，为 `UNKNOWN` |
| 账户风险 | `engine.risk.check_manual_plan`。账户状态的时效取最近一次 `watch` 快照的取数时刻；没有快照或过期为 `RISK_UNKNOWN_OR_STALE` |
| 纪律标记 | `COUNTERTREND_BEFORE_CONFIRMATION`、`RETRY_WITHOUT_NEW_SETUP`、`NO_STOP`、`RISK_OVER_BUDGET`、`DAILY_LIMIT_HIT` |

结论的合成：

1. 账户风险不通过 → `BLOCKED_BY_RISK`。
2. 否则一致性为 `ALIGNED` → `ALIGNED`。
3. 其余（`DEVIATES` 或 `UNKNOWN`）→ `DEVIATES`。

无法确认一致时不给 `ALIGNED`。`strategy_alignment` 仍单独如实报告 `UNKNOWN`。

`RETRY_WITHOUT_NEW_SETUP`：账户当日最近一笔平仓是止损出场，新计划与它同合约、同方向，而一致性评估没有给出对应的 `setup_id`。

预检结果按 `PretradeCheck` schema 校验后存进计划的 `pretrade_json`。

样例（牛 Spike 的回调中计划做空，由测试生成并逐字比对）：

```text
ESZ2025 做空 1 手，入场 5007.00，止损 5011.00，目标 4999.00。结论 DEVIATES：策略一致性 DEVIATES，账户风险 PASS；纪律标记 COUNTERTREND_BEFORE_CONFIRMATION。预检只给结论，不下单。
```

---

## 三、人工成交导入

```bash
uv run python -m trading_agent ingest --trades trades.csv --order-events orders.csv
```

**两个文件一起给时先导入订单事件**：成交的退出类型（是不是止损单成交）要用到它。先导成交、后补订单事件的话，已经登记的止损次数不会回头修改。

### 成交 CSV

必需列：`account_id`、`contract`、`side`（`BUY`／`SELL`）、`quantity`、`executed_at_utc`、`price`、`source`。
可选列：`broker_order_id`、`broker_fill_id`、`plan_id`。

缺必需列时整个文件拒收。单行解析不了（价格不在 tick 上、方向不认识、数量不为正）计入"无法解析"，不影响其他行。

### 订单事件 CSV

必需列：`account_id`、`broker_order_id`、`event_type`、`event_time_utc`、`source`。
可选列：`contract`、`side`、`order_kind`、`price`、`quantity`。

`event_type`：`SUBMITTED`、`MODIFIED`、`CANCELLED`、`TRIGGERED`、`FILLED`、`REJECTED`、`EXPIRED`。
`order_kind`：`STOP`、`STOP_LIMIT`、`LIMIT`、`MARKET`。

### 去重与隔离

- 有 `broker_fill_id`：按（来源，成交号）幂等。
- 没有：用全部字段合成一个键。同一文件里这个键只出现一次则导入，`match_rule = SYNTHETIC_KEY`；出现多次则无法唯一去重，这几行全部**隔离**（`quarantined = 1`），不参与任何统计。
- 重复导入同一文件：成交数量不变，账户止损次数不重复增加，已隔离的行不再重复隔离。

### 开平与盈亏

`manual_trades` 的一行是一笔成交的"开仓部分"或"平仓部分"，`side` 记的是仓位方向。

- 按账户和合约、按成交时间累计净持仓。使净持仓绝对值增加的是开仓，减少的是平仓。
- 跨过零的成交拆成平仓和开仓两行。
- 平仓的已实现盈亏按先进先出计算，减去 `config/risk.yaml` 里的往返费用。
- 每笔平仓登记到账户风险记录：对应订单是止损类型则计一次止损，否则只记盈亏。

请按时间顺序导入。

### 计划关联

只针对开仓：

| 情形 | `plan_link_status` |
| --- | --- |
| 成交带 `plan_id`，该计划在成交之前已激活且未结束，账户、合约、方向相符 | `LINKED` |
| 没有 `plan_id`，当时有恰好一份相符的已激活计划 | `UNKNOWN`，`suggested_plan_id` 给出待核对的那份 |
| 当时没有相符的已激活计划 | `UNPLANNED` |
| `plan_id` 指向的计划在成交之后才激活（事后补录），或不存在、不相符 | `UNPLANNED`，`link_note` 说明原因 |

"当时是否已激活"看的是 `plan_events` 审计记录，不是计划现在的状态。时间和合约吻合只用来给建议，不会自动建立关联。

---

## 四、纪律事件

`discipline_events` 每行一个事件：类型、动作时点、关联的计划和成交、`plan_adherence`、`strategy_alignment`、证据。

| 类型 | 判定 | 需要 |
| --- | --- | --- |
| `UNPLANNED` | 开仓成交的关联状态为 `UNPLANNED` | 成交、计划 |
| `COUNTERTREND_BEFORE_CONFIRMATION` | 关联计划的预检带这个标记 | 计划的预检结果 |
| `RETRY_WITHOUT_NEW_SETUP` | 关联计划的预检带这个标记；或没有计划的开仓发生在同合约同方向止损之后的冷却时间内 | 预检结果，或成交加订单事件 |
| `EARLY_EXIT` | 平仓价严格在计划的止损与目标之间，且平仓单不是止损类型 | 订单事件 |
| `STOP_WIDENED` | 持仓期间止损单被改到离入场更远 | 带时间和价格的订单修改事件 |
| `STOP_DELAYED` | 平仓价比计划止损差出超过 `stop_tolerance_ticks`（默认 2），且平仓单不是止损类型 | 订单事件 |
| `MISSING_EVIDENCE` | 有计划的平仓没有任何订单事件 | — |

要点：

- **止损单成交就是按计划止损**，哪怕因为跳空或滑点成交得比计划差。不记 `STOP_DELAYED`。
- **没有订单事件就不判**提前离场、放宽止损、延迟止损，只记一条 `MISSING_EVIDENCE`。
- **盈利的违规仍是违规**；按计划止损是合规。
- 每笔平仓的 `plan_adherence`：有上述违规为 `VIOLATION`；有关联计划、有订单事件、没有违规为 `COMPLIANT`；其余为 `UNKNOWN`。
- `strategy_alignment` 取关联计划预检时的值；没有计划为 `UNKNOWN`。
- 两者独立：完全按一份逆势的计划执行，是 `COMPLIANT` 加 `DEVIATES`。

### 交易员纠正

`manual.discipline.add_note(con, event_id, body, author)` 把纠正写进 `trader_notes` 并关联到事件。正文原样保存，是数据，不是指令；它不改变任何规则、状态或已有的记录。命令行入口等阶段 10 的控制通道。

---

## 五、watch

```bash
uv run python -m trading_agent watch --once --account U123
uv run python -m trading_agent watch --account U123 --interval 300
```

### 账户快照

适配器接口只有一个方法 `fetch(account_id)`。本阶段唯一的实现是 `FileSnapshotAdapter`，读：

```
<data_root>/broker/<account_id>/snapshot.json
```

```json
{
  "fetched_at_utc": "2026-11-23T15:30:00Z",
  "positions":   [{"contract": "ESZ6", "quantity": 1, "avg_price": 5000.25, "unrealized_usd": -120.5}],
  "open_orders": [{"order_id": "S1", "contract": "ESZ6", "side": "SELL", "kind": "STOP", "quantity": 1, "price": 4995}],
  "fills_today": []
}
```

`positions` 里的 `quantity` 有符号（负数是空仓）。`unrealized_usd` 可以不给。这个文件由你或别的工具写，`watch` 只读。真实券商的适配器等定了券商再加。

### 间隔、宽限、过期

- 轮询间隔：`config/watch.yaml` 的 `poll_interval_seconds`（默认 60），`--interval` 覆盖，任意正整数秒。
- 裸仓的宽限时间等于轮询间隔。
- 过期阈值：`max(2 × 间隔, 120)` 秒。
- 快照的取数时刻超过阈值，或读取失败：写一条 `WARN` 告警（`BROKER_DATA_STALE`），写明数据时点，**本轮不评估任何规则**。
- `stream_first` 目前没有流式适配器，实际只有轮询。
- 每轮结束更新心跳（`watch_state` 的 `heartbeat`），不管成功与否。

### 规则

| 规则 | 触发 | 级别 |
| --- | --- | --- |
| `NAKED_POSITION` | 有持仓，从首次看到它没有保护止损单起已超过宽限时间 | CRITICAL |
| `STOP_MOVED_AWAY` | 同一张止损单的价格相对上一快照向不利方向移动；或止损单消失而持仓仍在 | WARN |
| `REENTRY_IN_COOLDOWN` | 同合约同方向止损出场后的 `cooldown_minutes`（默认 15）内再次出现持仓。证据里列出有没有新的 setup、有没有已激活的计划、账户是否已停手 | WARN |
| `UNPLANNED_ENTRY` | 新出现的持仓找不到账户、合约、方向相符的已激活计划 | WARN |
| `COUNTERTREND_ADD` | 持仓增加，而策略的一致性评估认为这个方向逆势。没有可用的策略状态时不触发，在结果的 `unknown` 里记一条 | WARN |
| `DAILY_LOSS_NEAR_LIMIT` | 当日已实现加未实现亏损达到日损上限的 `daily_loss_warn_fraction`（默认 80%）。上限没配置或快照没有未实现盈亏时不评估，记入 `unknown` | WARN |

保护止损单：与持仓同合约、方向相反、类型为 `STOP` 或 `STOP_LIMIT`、数量不小于持仓。

规则可以在 `config/manual.yaml` 的 `rules` 里逐条关闭。`REENTRY_IN_COOLDOWN` 要靠导入的成交和订单事件才知道"刚止损过"。

### 提醒

- 写进 `discipline_alerts`（带快照 ID、关联计划、持仓键）和通用的 `alerts` 表。
- 文字只陈述事实和规则名，不给交易指令。
- 同一持仓、同一规则在 `quiet_window_minutes`（默认 15）内不重复。
- 条件消失（补上了止损、持仓没了）时把提醒标为已解除并记时刻。
- 本阶段的通知器只是打印。阶段 10 换成控制通道。

样例（由测试生成并逐字比对）：

```text
ESZ2025 多仓 1 手，已 61 秒没有对应的保护止损单（规则 NAKED_POSITION，宽限 60 秒）。
```

---

## 六、每日指标

每个账户每个交易日一条，写在 `sessions`（`session_id = manual:<账户>:<交易日>`）的 `kpi_json` 里。导入成交时更新。

| 指标 | 口径 |
| --- | --- |
| `plan_coverage` | 关联状态为 `LINKED` 的开仓数 ÷ 全部开仓数。隔离的成交不计。没有开仓时为空 |
| `sample_insufficient` | `plan_coverage` 低于 `plan_coverage_threshold`（默认 0.8）。为真时，当日的纪律结论应标注样本不足 |
| `opens_planned`、`opens_unplanned`、`opens_link_unknown` | 开仓按关联状态分列 |
| `adherence_decidable`、`adherence_unknown`、`adherence_violations` | 平仓按 `plan_adherence` 分列。可判定和未知分开 |
| `discipline_events` | 每类纪律事件的次数 |
| `alerts` | 每类提醒：次数、已解除数、从触发到解除的中位秒数、仍未解除的比例 |
| `quarantined_trades` | 被隔离的成交数 |

---

## 七、配置

`config/manual.yaml`：

| 项 | 默认 | 含义 |
| --- | --- | --- |
| `default_account_id` | 空 | 默认账户。留空时 `plan`、`watch` 必须带 `--account` |
| `plan_ttl_minutes` | 30 | 计划多久之后失效 |
| `cooldown_minutes` | 15 | 止损后的冷却时间 |
| `quiet_window_minutes` | 15 | 提醒的静默窗口 |
| `plan_coverage_threshold` | 0.8 | 覆盖率阈值 |
| `stop_tolerance_ticks` | 2 | 拖止损的容差 |
| `daily_loss_warn_fraction` | 0.8 | 日损提醒的比例 |
| `strategy_state_max_age_bars` | 2 | 策略状态落后几根 K 就视为不可用 |
| `rules` | 全开 | `watch` 各条规则的开关 |

---

## 八、股票类品种与 Alpaca 模拟盘（2026-11-23）

人工轨现在也支持股票和 ETF。登记了 SPY、QQQ，默认账户是 Alpaca 模拟盘 `alpaca-paper`。

**只进人工轨。** SPY、QQQ 没有策略 scope、没有回放、没有基线。正式策略只在 ES 上定义；不拿 ES 的信号去评价 SPY 或 QQQ 的交易。所以：

- 预检的"策略一致性"对它们一律是 `UNKNOWN`，摘要里写明"该品种没有正式策略，不评价"。结论沿用既有规则：风险通过时为 `DEVIATES`。它在这里的意思是"无法确认与策略一致"，不是"你做错了"。
- `watch` 里依赖策略状态的规则（逆势加仓、冷却期内是否有新 setup）对它们不评估，记入"无法评估"。
- 纪律指标照常：计划覆盖率、无计划成交、止损后重试、提前出场、拖止损、放宽止损。

**写法**与期货相同，数量是股数，价格精确到分：

```bash
uv run python -m trading_agent plan SPY L 700.50 699.00 705 100 回调买入
uv run python -m trading_agent plan --activate plan_xxxxxxxxxxxx
uv run python -m trading_agent watch --once
uv run python -m trading_agent ingest --alpaca            # 导入 Alpaca 的订单记录和成交
```

每股计划风险 = 止损距离 + 滑点预算（2 tick，即 2 分钱），Alpaca 零佣金。上例是 1.50 + 0.02 = 1.52 美元，100 股共 152 美元。

**账户限额**在 `config/accounts.yaml`，按账户分开，不写进 `config/risk.yaml`，所以不影响纸面评测的 `risk_version` 和基线。`alpaca-paper` 的限额（2026-11-23 用户确认，账户权益约 98,000 美元）：单笔预算 250 美元，日损上限 1,000 美元，最大 200 股，每日最多 2 次止损。没有登记的账户沿用 `risk.yaml` 的 `account` 一节。

**读 Alpaca 账户**有两种方式，解析相同，都是只读：

- 有 API 密钥（按顺序找：环境变量、`<data_root>/secrets/alpaca.env`、项目根目录 `.env` 里的 `ALPACA_API_KEY`、`ALPACA_SECRET_KEY`）：直接读。2026-11-23 起放在 `~/trading-agent-data/secrets/alpaca.env`（权限 600，不在项目里）。只发 GET，只连模拟盘地址 `paper-api.alpaca.markets`。
- 没有密钥：读 `<data_root>/broker/alpaca-paper/alpaca/` 下保存的 JSON：`positions.json`、`orders_open.json`、`orders_all.json`，可选 `fills.json`。在 Claude 会话里用 Alpaca 连接器取回后存下即可。

`plan` 在登记和激活前会先读一次账户，因为预检要求账户状态足够新（默认 120 秒内）。

**口径**：

- 只处理已登记的代码。账户里别的股票和碎股忽略并计数。
- 成交价和均价可能带到小数点后四位，按最近的一分钱取整。
- 没有逐笔成交记录（`fills.json`）时，用订单记录里已成交的订单，一张订单算一笔，价格是订单的成交均价。
- Alpaca 改单是"替换成一张新订单"。整条替换链记在最后那张订单的编号下：最初那张记为 `SUBMITTED`，之后每次替换记为 `MODIFIED`（带新价格）。"放宽止损"据此判定。
- **计划关联的凭据是订单的 `client_order_id`**：里面含有计划编号（`plan_xxxxxxxxxxxx`）就算可证明的关联（`LINKED`）。否则只按时间吻合记为待核对（`UNKNOWN`），并给出建议的计划。计划覆盖率只数 `LINKED`。
- 没有事前计划的成交只记 `UNPLANNED`；提前出场、拖止损、放宽止损都是相对计划而言，没有计划就不判。
- 交易日按期货的口径切分（18:00 ET）。18:00 之后的盘后成交会记到下一个交易日。
- 订单记录一次最多取 500 张。更早的成交不在其中时，最早的几笔平仓会找不到对应的开仓。

## 九、假设

1. **一致性 `UNKNOWN` 时结论给 `DEVIATES`**。偏保守。
2. **历史 `scan` 用一次性的运行**，`run_id` 由 scope、时点、策略版本、风控版本决定，重复调用复用同一个运行。
3. **数据异常时策略照常运行**以保持状态连续，但引擎因数据阻止不挂新单，对外的结果是 `DATA_ERROR`。
4. **`scan` 的数据校验不再单独写扫描记录**：扫描记录由引擎那一步写。直接调用 `scan.run_scan` 仍会写。
5. **开平判定用净持仓，盈亏用先进先出**。与券商的口径可能不同，只影响盈亏归属到哪一笔，不影响总额。
6. **平仓部分的计划**取它所平的开仓批次的计划；这些批次的计划不唯一时不关联。
7. **`STOP_WIDENED` 每笔平仓最多记一条**，取第一次不利修改。
8. **没有计划的平仓不判执行情况**，`plan_adherence = UNKNOWN`。
9. **已 `FILLED` 的计划仍算"有计划"**：`watch` 看到的持仓对应一份已成交的计划时不报 `UNPLANNED_ENTRY`。
10. **`watch` 的"新开仓"和"加仓"靠前后两次快照比较**。两次采集之间开了又平的仓位看不到；首次运行时已有的持仓会被当作新出现的。
11. **日损提醒里的已实现亏损**来自导入的成交，不是来自快照。
12. **纠正的命令行入口**留给阶段 10。
