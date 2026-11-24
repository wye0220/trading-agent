# Memory 存储（阶段 7）

Memory 是独立的版本化对象：已确认的偏好、有有效期的市场事实、有证据的个人执行模式。条目只由结构化字段组成，没有自由文本入口，所以指令和授权写不进去。

它不调用模型，不读风控配置、账本里的仓位或保留集。Memory 只影响解释和提醒，不参与 Python 的信号、风控、成交和评测。

## 命令

```bash
uv run python -m trading_agent memory                        # 当前版本、生效条目、待处理的候选
uv run python -m trading_agent memory --confirm memc_…       # 确认一条候选
uv run python -m trading_agent memory --reject memc_… --reason WRONG
uv run python -m trading_agent memory --supersede mi_… --evidence <事件标识>   # 条目被新证据否定
uv run python -m trading_agent memory --rollback mem_…       # 回到一个已有的版本
uv run python -m trading_agent memory --history              # 最近的事件
uv run python -m trading_agent memory --context SessionReview   # 某类任务会注入哪些条目
```

## 条目

| 字段 | 含义 |
| --- | --- |
| `item_id` | 由 kind、scope、subject、condition、metric 的摘要得出。同一件事的新值是同一个 `item_id` 的下一个 `version` |
| `kind` | `user_preference`、`market_fact`、`discipline_pattern` |
| `scope` | 已登记的四维 scope，或 `global` |
| `statement` | 结构化对象，见下 |
| `source_event_ids` | 证据的事件标识 |
| `confirmed_by` | `user:cli`、`user:control`，或 `evaluation:<评测标识>` |
| `created_at`、`valid_until` | UTC 时刻；`valid_until` 可为空（市场事实必填） |
| `status` | `ACTIVE`、`SUPERSEDED`、`EXPIRED` |
| `version` | 该条目的第几个版本，从 1 起 |

`statement` 的字段，每个字符串都是枚举或固定文法：

| 字段 | 允许的内容 |
| --- | --- |
| `subject` | `user_preference`、`trader`、`instrument.<根代码小写>` |
| `condition` | 空，或用 `.` 连接的 `键=值`（数值键可用 `>=`、`<=`、`>`、`<`），如 `event=retry_without_new_setup.attempt>=2.cycle=bear_spike` |
| `metric` | 该 kind 登记的指标；偏好是白名单里的键 |
| `value` | 数。偏好是登记范围内的整数；比例在 0 到 1 之间；日期写成 `yyyymmdd` 的整数 |
| `unit` | 空、`s`、`yyyymmdd`、`ratio`、`r`、`count`，必须与指标登记的一致 |
| `sample_n` | 样本数（执行模式必填） |
| `period` | `YYYY-MM-DD..YYYY-MM-DD`（执行模式必填） |
| `source` | `trader_note`、`session_review`、`evolve_cluster`、`manual_ledger`、`exchange_calendar`、`instrument_config` |

登记表在 `config/memory.yaml`：偏好键、指标、条件键及其取值、来源、每类任务相关的条目种类。

## 校验

两层，候选提出、确认和加载时各做一次：

1. **JSON schema**（`schemas/MemoryCandidate.schema.json`、`schemas/MemoryItem.schema.json`）：类型、长度、字符集，不允许多余字段。带空格、中文或标点的文本过不了这一层。
2. **登记表**：名字和取值必须已登记。形如标识符的"指令"（如 `ignore_all_rules`）过得了 schema，过不了这一层。

| 原因码 | 含义 |
| --- | --- |
| `SCHEMA_INVALID` | 没通过 schema，或日期、区间不合法 |
| `RISK_LIMIT_FORBIDDEN` | 名字指向风险上限或权限（预算、限额、最大手数、批准、发布等） |
| `LIVE_STATE_FORBIDDEN` | 名字指向实时状态（仓位、盈亏、已占用风险、余额等） |
| `SCOPE_NOT_REGISTERED`、`SUBJECT_NOT_REGISTERED`、`METRIC_NOT_REGISTERED`、`CONDITION_NOT_REGISTERED`、`SOURCE_NOT_REGISTERED` | 引用了未登记的内容 |
| `UNIT_MISMATCH`、`VALUE_OUT_OF_RANGE` | 单位或取值不符 |
| `EVIDENCE_INSUFFICIENT` | 执行模式的样本数不到 20，或缺统计区间、证据 |
| `VALID_UNTIL_REQUIRED`、`ALREADY_EXPIRED` | 市场事实没有有效期；有效期已过 |

两类禁止项写在代码里（`memory/registry.py`），先于白名单检查。`config/memory.yaml` 里登记这类名字时，配置加载直接失败，所以白名单扩不到它们。当前仓位、风险累计和风险上限只从账本和风控配置读。

## 生命周期

```text
候选（6A 的备注偏好；6B 的人工轨聚类）  PENDING_CONFIRMATION
  ├─ 确认 user_preference                → 条目 ACTIVE，候选 CONFIRMED
  ├─ 确认 market_fact／discipline_pattern → PENDING_EVALUATION
  │     └─ 阶段 8 评测通过                → 条目 ACTIVE，confirmed_by = evaluation:<标识>
  └─ 拒绝                                 → REJECTED
条目：新值 → 旧版本 SUPERSEDED；被新证据否定 → SUPERSEDED；过了 valid_until → 不再加载
```

- 没有确认者的条目不存在。未经确认的推测只能停在候选。
- `market_fact` 和 `discipline_pattern` 的生效函数要求 `evaluation_runs` 里有该候选 `status='PASSED'` 的记录。阶段 7 没有任何入口能写出这条记录，所以这两类现在只能走到 `PENDING_EVALUATION`。
- 否定市场事实或执行模式要给证据标识；偏好可以直接撤回。
- 每次变动在 `memory_events` 追加一条（只追加，数据库触发器拒绝改和删）。

## 版本与回滚

`memory_version` 是一个快照：成员是 `(item_id, version)` 的有序列表，标识为 `mem_<成员列表 sha256 的前 16 位>`。快照不可变；相同的成员集得到相同的标识。

头指针只有一个（`memory_head`），每次确认、否定、回滚都在同一个事务里重读并按版本号更新，过期的写入被拒。

回滚只把头指针指回旧快照，条目的行不动。之后的加载按那个快照的成员取，所以回滚后读到的是旧值。任何历史版本都可以按标识加载。

过期是加载时的过滤，不改快照：`memory_version` 不会在没有人操作时变化。`memory` 命令顺带把过期条目的状态标成 `EXPIRED`，只影响展示。

## 加载与 Context

`memory.context.load(scope, memory_version=None)` 返回该版本下 scope 为 `global` 或当前四维 scope、且没过期的条目，以及 `scope_digest`（实际加载内容的摘要）和被排除的计数（`EXPIRED`、`INVALID`、`MISSING`）。

`memory.context.build(scope, task)` 只注入登记为与该任务相关的条目，最多 12 条：

| 任务 | 注入 |
| --- | --- |
| `ScanExplanation` | 偏好 `report_max_lines`、市场事实 |
| `PretradeCheck`、`SessionReview` | 偏好 `report_max_lines`、市场事实、执行模式 |
| `Candidate` | 执行模式 |
| `MemoryCandidate` | 市场事实、执行模式 |
| `ReleaseReview` | 无 |
| `watch` | 偏好 `watch_poll_interval_seconds` |

每条给出 `item_id`、`kind` 和模板生成的句子。句子只由字段和登记表里的标签拼出，末尾带 `[item_id]`，例如：

```text
执行模式（全局）：事件为 retry_without_new_setup、第几次尝试不小于 2、周期为 bear_spike时，失败率（failure_rate）为 60%（样本 40，区间 2026-01-05..2026-06-30，来源 manual_ledger） [mi_…]
```

`check_citations(输出, context)` 核对输出里出现的 `item_id` 都属于本次注入，否则是 `UNKNOWN_MEMORY_CITATION`。

## 已接入的地方

- `watch` 的轮询间隔：命令行 `--interval` → 已生效的偏好 `watch_poll_interval_seconds` → `config/watch.yaml`。
- 人工计划的版本元组带上当时的 `memory_version`。
- `evolve` 为可升级、有暴露条件的人工轨聚类生成 `discipline_pattern` 候选（出现天数占比）。暴露天数不到 20 的跳过，周报里计数。

## 假设

以下是我定的保守解，需要你确认。

1. **本机 CLI 的 `memory --confirm` 视为你的确认**（`confirmed_by = user:cli`）。控制通道在阶段 10，接上后改记 `user:control`。
2. **一个全局头指针**，加载时按条目的 scope 过滤。每个 scope 钉住哪个 `memory_version`，等阶段 9 写进发布清单。
3. **引擎运行的版本元组里 `memory_version` 仍为空。** 填它要改 `engine/`，会让基线失效；Memory 也不影响引擎的任何决策。阶段 9 由发布清单带入。
4. **登记表是我写的预设**：2 个偏好键、2 个市场事实指标、5 个执行模式指标、5 个条件键、9 种事件。表达不了的事实进不了 Memory；要加就改 `config/memory.yaml`。
5. **执行模式至少 20 个样本**（`discipline_min_sample_n`）。
6. **评测通过的记法**是 `evaluation_runs.status = 'PASSED'`，阶段 8 定稿时可能要改。
7. **`report_max_lines` 还没有消费者**：确认后会进 Context，但报告的行数现在不受它控制。
8. **`MemoryCandidate` schema 收紧了**：`statement` 的 8 个字段和 `valid_until` 全部必填；`scope`、`unit`、`period`、`source`、证据标识改为枚举或固定格式。6A 生成的候选本来就符合。
9. **人工轨的执行模式用 `global` scope**：条目的 scope 只能是四维 scope 或 `global`，账户不是其中一维。
10. **市场事实没有生成器。** 换月日之类的事实现在由代码直接算，没有写进 Memory 的入口；`propose` 接口留给阶段 8 之后用。
