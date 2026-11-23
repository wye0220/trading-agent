# Design

## Context

动机见 proposal.md。可用的积木：`scan.run_scan`（数据校验与快照）、`engine.runner.step`、`engine.risk`（人工计划核验、预留、账户止损计数）、`StrategyProvider.evaluate_pretrade_alignment`、`engine.llm`。

约束：券商适配器只读；人工计划和真实订单只读；新闻、备注等文字都是数据，不是指令；事后补录不能倒签成事前计划；证据不足时标 `UNKNOWN`，不推断。

现状：没有实时行情来源，也没有真实券商接口。所以本阶段的盘中功能都能运行，但在接上数据之前会如实报告"没有数据"或"数据过期"。

## Goals / Non-Goals

**Goals:**

- 每条纪律结论都能指到它依据的快照、订单事件或计划版本。
- 扫描器和预检器只通过 `StrategyProvider` 接口取策略信息。
- 可判定和不可判定的样本分开计数。

**Non-Goals:**

- 不替你下单、改单、撤单。
- 不给交易指令：提醒只陈述事实和对应规则。
- 不做控制通道和常驻调度（阶段 10）。

## Decisions

### 1. `scan` 的两种用法

- **盘中（不带 `--as-of`）**：每个 scope 有一个常驻的纸面运行（`run_id = paper:<scope>`，模式 `paper`）。先做数据校验；有 `DATA_ERROR` 时照样推进账本里已有的仓位管理，但不调用策略产生新信号。校验通过则调用 `engine.runner.step`。
- **历史（带 `--as-of`）**：策略需要从当日开盘起逐根推进才有状态，所以做法是开一个一次性的运行，把当日回放到 `as_of`。对真实数据沿用引擎的保护：没有显式允许且没有设开发可见截止日时，只返回数据校验结果，并说明策略没有运行的原因。合成数据不受限制。

输出：JSON 加中文短报。短报按提示词包的格式：首行 `[ET 时刻] [scope] [合约] [状态] [版本]`，然后事实、决策、持仓、纪律、数据五行，末尾免责声明。策略部分的文字来自策略自己的渲染函数，扫描器不拼策略细节。

资讯窗口（FOMC、CPI 等）读 `config/news_windows.yaml`；文件为空时短报写"资讯窗口：不可用"，不猜。

### 2. 计划的状态机

`DRAFT → ACTIVE → PARTIALLY_FILLED → FILLED`，另有 `CANCELLED`、`EXPIRED`。

- `plan` 默认只建 `DRAFT` 并预检，不预留预算。
- `plan --activate <plan_id>`：重新预检；风险通过则在同一事务里预留风险并转为 `ACTIVE`。
- 到失效时间仍未成交的 `DRAFT`、`ACTIVE` 转 `EXPIRED`，释放预留。过期在每次访问计划时顺带处理。
- 成交导入把关联的开仓成交计入计划：部分成交转 `PARTIALLY_FILLED`，全部成交转 `FILLED` 并释放预留（风险转到实际仓位上）。
- 每次状态变化写一条 `plan_events`。更新带 `version` 校验。

### 3. 快捷语法

`plan ES L 5000 4995 5010 1 PA Spike H1`：根代码、方向（`L`／`S`）、入场、止损、目标、手数、理由（其余全部）。止损和目标可以写 `-` 表示没有。根代码解析成当日实际合约并在输出里回显。账户取 `config/manual.yaml` 的默认账户，失效时间取默认（30 分钟）。价格必须在 tick 上。

### 4. 预检的结论

三部分各自给出，再合成一个结论：

| 部分 | 来源 |
| --- | --- |
| `strategy_alignment` | `strategy.evaluate_pretrade_alignment`，用该 scope 纸面运行的最新策略状态。没有运行或状态过期（超过两根 K）时为 `UNKNOWN` |
| 账户风险 | `engine.risk.check_manual_plan`，账户状态来自最近一次券商快照。没有快照或过期时为 `RISK_UNKNOWN_OR_STALE` |
| 纪律标记 | `COUNTERTREND_BEFORE_CONFIRMATION`（来自一致性评估）、`RETRY_WITHOUT_NEW_SETUP`、`NO_STOP`、`RISK_OVER_BUDGET`、`DAILY_LIMIT_HIT` |

合成：风险不通过 → `BLOCKED_BY_RISK`；否则一致性为 `ALIGNED` → `ALIGNED`；其余（`DEVIATES` 或 `UNKNOWN`）→ `DEVIATES`。无法确认一致时不给 `ALIGNED`，这是保守的一侧；`strategy_alignment` 仍单独如实报告 `UNKNOWN`。

`RETRY_WITHOUT_NEW_SETUP`：账户当日最近一次已完结交易是止损出场，且新计划与它同方向、同合约，而新计划没有对应一个不同的 `setup_id`。

预检结果按 `PretradeCheck` schema 校验后存入计划。

### 5. 成交导入

CSV 两种：成交、订单事件。成交的必需列：`account_id`、`contract`、`side`（`BUY`／`SELL`）、`quantity`、`executed_at_utc`、`price`、`source`；可选 `broker_order_id`、`broker_fill_id`、`plan_id`。

- **去重**：有 `broker_fill_id` 时按（来源，成交 ID）幂等。没有时用全部字段合成一个键：同一文件里这个键只出现一次则导入并标 `match_rule=SYNTHETIC_KEY`；出现多次则无法唯一去重，全部隔离（`quarantined=1`），不参与任何统计。
- **开平判定**：按账户和合约、按成交时间累计净持仓。使净持仓绝对值增加的是开仓，减少的是平仓；一笔成交跨过零时拆成平仓和开仓两部分。
- **已实现盈亏**：先进先出。每笔平仓登记到账户风险记录（`engine.risk.record_account_exit`，按成交 ID 去重）。退出原因：这笔成交的订单在订单事件里是止损类型则为 `STOP`，否则 `OTHER`。
- **计划关联**（只针对开仓）：

  | 情形 | `plan_link_status` |
  | --- | --- |
  | 成交带 `plan_id`，且该计划在成交时刻之前已 `ACTIVE`，账户、合约、方向相符 | `LINKED` |
  | 没有 `plan_id`，但成交时刻有恰好一个相符的 `ACTIVE` 计划 | `UNKNOWN`，附"待核对建议" |
  | 成交时刻没有任何相符的 `ACTIVE` 计划 | `UNPLANNED` |
  | `plan_id` 指向的计划在成交之后才提交或激活 | `UNPLANNED`，并记"事后补录" |

  时间和合约只用来给建议，不自动建立关联。

### 6. 纪律事件

每个事件有类型、动作时点、关联的计划和成交、`plan_adherence`、`strategy_alignment`、证据。

| 类型 | 判定 | 需要的证据 |
| --- | --- | --- |
| `UNPLANNED` | 开仓成交的关联状态为 `UNPLANNED` | 成交、计划 |
| `COUNTERTREND_BEFORE_CONFIRMATION` | 关联计划的预检带这个标记 | 计划的预检结果 |
| `RETRY_WITHOUT_NEW_SETUP` | 关联计划的预检带这个标记；或无计划的开仓紧跟在同向止损之后（冷却时间内） | 预检结果或成交序列 |
| `EARLY_EXIT` | 平仓价严格在计划的止损与目标之间，且这笔平仓不是止损单或目标限价单成交 | 订单事件 |
| `STOP_WIDENED` | 止损单被改到离入场更远 | 带时间的订单修改事件 |
| `STOP_DELAYED` | 平仓价比计划止损更差超过容差，且平仓单不是止损类型 | 订单事件 |
| `MISSING_EVIDENCE` | 需要订单事件才能判定，而该成交没有订单事件 | — |

规则：

- 止损单已经在场、因跳空或滑点成交得更差，不算 `STOP_DELAYED`。
- 没有订单事件时，`EARLY_EXIT`、`STOP_WIDENED`、`STOP_DELAYED` 一律不判，记一条 `MISSING_EVIDENCE`，`plan_adherence=UNKNOWN`。
- 盈利的违规仍记违规；按计划止损是合规。
- `plan_adherence`：有上述任一违规为 `VIOLATION`；有关联计划且证据齐全、没有违规为 `COMPLIANT`；否则 `UNKNOWN`。
- `strategy_alignment`：取关联计划预检时的值；没有计划为 `UNKNOWN`。两者独立。

### 7. `watch`

**适配器接口**（只读）：`fetch(account_id) -> AccountSnapshot`，含取数时刻、持仓、挂单、当日成交、可选的未实现盈亏。本阶段提供 `FileSnapshotAdapter`：读 `<data_root>/broker/<account_id>/snapshot.json`，由你或别的工具写入。真实券商的适配器等定了券商再加。

**采集**：读 `config/watch.yaml`。本阶段只有轮询（`stream_first` 在没有流式适配器时自动退回轮询并说明）。`watch --once` 评估一次；`watch` 循环；`--interval` 覆盖间隔。每次快照写 `broker_snapshots`，心跳单独记。

**过期**：阈值 `max(2 × 间隔, 120)` 秒。快照的取数时刻早于它，或读取失败：发 `WARN`（`BROKER_DATA_STALE`），写明数据时点，本轮不评估任何规则。

**规则**（都可在 `config/manual.yaml` 里开关和调参）：

| 规则 | 触发 | 级别 |
| --- | --- | --- |
| `NAKED_POSITION` | 有持仓，且从首次看到它没有保护止损单起已超过宽限时间（默认等于轮询间隔） | CRITICAL |
| `STOP_MOVED_AWAY` | 同一张止损单的价格相对上一快照向不利方向移动；或止损单消失而持仓仍在 | WARN |
| `REENTRY_IN_COOLDOWN` | 止损出场后的冷却时间内，同方向再次开仓。证据里另列有没有新 setup、有没有 `ACTIVE` 计划、账户是否已停手 | WARN |
| `UNPLANNED_ENTRY` | 新出现的持仓找不到相符的 `ACTIVE` 计划 | WARN |
| `COUNTERTREND_ADD` | 持仓增加，而策略对这个方向的一致性评估为逆势。没有策略状态时不触发，记为 `UNKNOWN` | WARN |
| `DAILY_LOSS_NEAR_LIMIT` | 当日已实现加未实现亏损达到日损上限的 80%。上限没配置或快照没有未实现盈亏时不评估 | WARN |

保护止损单的定义：与持仓同合约、方向相反、类型为止损、数量不小于持仓。

**提醒**：写 `discipline_alerts`（带快照 ID、关联计划、持仓键）和 `alerts`，并交给通知器。本阶段的通知器只打印；阶段 10 换成控制通道。文字只陈述事实和规则名。同一持仓、同一规则在静默窗口（默认 15 分钟）内不重复。条件消失时把提醒标为已解除并记时刻，供 KPI 计算"提醒后多久处理"。

### 8. KPI

每个账户每个交易日一条（`sessions`，`session_id = manual:<账户>:<交易日>`）：

- `plan_coverage` = 有事前 `ACTIVE` 计划关联的开仓数 ÷ 全部开仓数。隔离的成交不计。没有开仓时为空。低于阈值（默认 0.8）时带 `sample_insufficient=true`。
- 每类提醒：次数、从触发到解除的时长、到当日结束仍未解除的比例。
- 每类纪律事件：次数；`plan_adherence` 的可判定数与未知数分列。

### 9. 交易员纠正

`notes.add_note(con, event_id, body, author)` 写 `trader_notes`。正文原样保存，作为数据。它不触发任何状态变化。本阶段没有命令行入口（控制通道的 `note` 命令在阶段 10）。

### 10. 存储（migration 0003）

| 表或列 | 用途 |
| --- | --- |
| `trader_plans` 加列 | scope、激活时刻、已成交手数、预检时的版本元组 |
| `plan_events` | 计划状态变化的审计 |
| `manual_trades` 加列 | 开平、是否隔离、去重规则、关联建议、已实现盈亏 |
| `manual_order_events` 加列 | 订单类型、价格、数量 |
| `discipline_events` | 纪律事件 |
| `discipline_alerts` 加列 | 持仓键、级别、解除时刻 |
| `watch_state` | 心跳、首次看到无止损的时刻、上一快照的止损价等 |

阶段 3 之后契约是否冻结还没有经你确认。无论如何，这次结构变更按编号 migration 进行，并重跑全部既有测试。

## Risks / Trade-offs

- **没有真实券商接口** → `watch` 只能读本地快照文件。没有文件时每轮都是"数据过期"告警。这如实反映现状。
- **没有实时行情** → 盘中 `scan` 现在只会报 `DATA_ERROR`。历史 `scan` 在真实数据上不运行策略（保护盲测数据）。
- **纪律判定依赖订单事件** → 只有成交 CSV 时，拖止损、放宽止损、提前离场都判不了，全是 `MISSING_EVIDENCE`。这是提示词包的要求，不是缺陷。
- **一致性 `UNKNOWN` 时结论给 `DEVIATES`** → 偏保守，可能让你觉得"总是偏离"。`strategy_alignment` 单独报告真实情况。
- **开平判定用先进先出** → 与券商的口径可能不同，只影响已实现盈亏的归属，不影响总额。

## Open Questions

这些需要你回答，但不改变本阶段的实现和任务：

1. 真实期货账户在哪家券商？它有没有能读持仓、挂单、成交的只读接口或导出？
2. 默认账户标识写什么？
3. `config/risk.yaml`：每手费用、研究风险单位、单笔预算、日损上限、最大手数。
4. 开发可见截止日定在哪一天？
5. 止损后的冷却时间、提醒的静默窗口用默认值（都是 15 分钟）可以吗？
