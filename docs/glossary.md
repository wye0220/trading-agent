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
- **换月规则**：版本化，登记在 `config/instruments.yaml`。`roll-v1`：到期日前第 8 个日历日（前一周的星期四）所在交易日起，实际合约切到下一季月。
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

## 数据位置

- **数据根目录（`data_root`）**：项目之外，默认 `~/trading-agent-data/`。存放原始行情（`databento/`、`webull/`）和运行库（`runtime/`）。
- **运行库**：`<data_root>/runtime/trading_agent.sqlite`，全部业务表。
- **发布库**：项目内 `releases/release-state.sqlite`，只有 active 指针和审计事件，只有发布入口可写。
- **保留集**：项目之外的独立路径，只在 `config/paths.yaml` 登记。

## 快照

一次扫描所用数据的封存记录，带唯一 ID 和内容摘要。相同输入得到相同摘要。

## 数据异常类别

见 `docs/data-contract.md` 的「校验规则与异常类别」。
