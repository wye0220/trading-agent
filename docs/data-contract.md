# 数据合同（阶段 1）

术语见 `docs/glossary.md`。本文件描述行情如何进入系统、以什么形式保存、如何校验，以及下游按什么规则读取。

## 数据根目录

真实行情和运行库都在项目之外。`config/paths.yaml` 的 `data_root` 默认 `~/trading-agent-data`，可用环境变量 `TRADING_AGENT_DATA_ROOT` 覆盖。配置成项目目录内的路径时，程序拒绝运行。

```
~/trading-agent-data/
  databento/GLBX-20260905-AKYFXCHGY4/     Databento 批量下载（ES.FUT ohlcv-1m，2019-09-05 至 2026-09-04）
  webull/<品种>/<contract_id>/<交易日>.csv  collect 追加
  runtime/trading_agent.sqlite            运行库
```

`ingest` 递归扫描数据根目录，跳过 `runtime/` 和隐藏目录，识别 `*.dbn`、`*.dbn.zst`、`*.csv` 和 `condition.json`。它不修改、不移动、不删除任何来源文件，也不发起网络请求。

项目内的 `data/` 只有一份说明。合成样本在 `tests/fixtures/`。

## K 线合同

每根入库的 K 线（表 `bars`）：

| 字段 | 说明 |
| --- | --- |
| `source` | 来源：`databento`、`webull`，或 CSV 里写明的其他来源 |
| `instrument_root`、`contract_id` | 品种根代码、实际合约（四位年份，如 `ESH2027`） |
| `timeframe` | `1m`、`5m`、`1h` |
| `bar_start_utc`、`bar_end_utc` | 起止时刻，UTC epoch 秒 |
| `trading_day`、`session_type` | 交易日；`RTH`、`ETH`，1 小时线跨时段时为 `MIX` |
| `open`、`high`、`low`、`close` | 整数 tick |
| `volume` | 整数 |
| `available_at_utc`、`available_at_estimated` | 该 revision 最早可见的时刻，以及它是否为估计值 |
| `revision` | 从 1 起。主键是 `(source, contract_id, timeframe, bar_start_utc, revision)` |
| `derived`、`constituent_count`、`deps_json` | 派生线专用：参与聚合的下级 K 线数，以及它们的 `[起始时刻, revision]` |
| `fixture` | 合成样本标记 |
| `file_id` | 指向 `ingest_files`，可追溯到具体文件 |

价格不能被最小变动价位整除时记 `OFF_TICK_PRICE`，该行不入库，不四舍五入。

## 来源适配器

### Databento DBN

- 只支持 `ohlcv-1m`。其他 schema 的文件登记为 `UNSUPPORTED_SCHEMA`，不导入，不影响其他文件。
- 用 `databento-dbn` 本地流式解码，支持 `.zst`。不需要 API key。
- 合约由文件头的符号映射确定。记录的日期不在其 instrument 的任何映射区间内时，记 `UNMAPPED_INSTRUMENT`。
- **价差合约丢弃**：代码含 `-` 的是日历价差，它的"价格"是价差。导入摘要报告丢弃的代码数和记录数。
- **短代码规范化**：来源的年份只有一位，`ESH0` 既指 2020 年 3 月也指 2030 年 3 月。规则：取映射区间的起始日期，找不早于它、个位数一致且尚未到期的第一个年份。
- `ts_event` 是 1 分钟线的起始时刻。没有成交的分钟没有记录。
- 没有到达时刻：`available_at_utc = bar_end_utc + available_at_delay_seconds`，`available_at_estimated=1`。
- 同目录的 `condition.json` 是逐日质量状态，读入 `day_quality`。状态不是 `available` 的交易日标为降级日。

### CSV

必需列：`instrument_root`、`contract_id`、`timeframe`、`bar_start_utc`、`open`、`high`、`low`、`close`、`volume`、`source`。缺任一列，整个文件拒收并指出缺少的列。

可选列：

- `fetched_at_utc`：取数时刻。有它时，`available_at_utc` 取该 K 线最早的取数时刻，`available_at_estimated=0`。
- `fixture`：`1` 表示合成样本。文件名含 `fixture` 的文件也整体视为合成样本。
- `bar_end_utc`：只供人阅读，入库时按周期重新计算。

时刻是 ISO 8601，不带时区按 UTC。价格是十进制文本。`timeframe` 支持 `1m` 和 `5m`。`contract_id` 可以写短代码，入库时规范化。单行解析失败记 `BAD_ROW`，不影响其他行。

### Webull 来源

> **状态（2026-11-22）：不可用，暂缓。** Webull 账号没有美国期货行情，取不到 ES 的 K 线。用户决定先不接实时来源，阶段 2–5 只用历史数据；Webull 适配器保留，等做到盘中 `scan`、`watch` 时再定来源。下面的采集规则与来源无关，换适配器后照样适用。

`collect` 用 Webull OpenAPI SDK 的行情接口 `futures_market_data.get_futures_history_bars`，类别 `US_FUTURES`，周期 `M5`，一次最多 1200 根。

**核实状态**（2026-11-22 用真实凭证试过；因为账号没有期货行情订阅，下表除第一行外仍未核实）：

已确认的一点：`get_futures_history_bars` 的 `symbols` 要传字符串。SDK 不会替列表拼接，传列表时服务器返回 `UNAUTHORIZED: Header x-signature is invalid`。适配器已改为传字符串。

| 项目 | 目前的做法 | 核实后可能要改的地方 |
| --- | --- | --- |
| 账号能否取到 ES 期货 K 线 | **2026-11-22 实测：不能。** 凭证有效（股票快照返回 200），但期货快照和期货 K 线都返回 `MARKET_DATA_NOT_SUBSCRIBED`（需要订阅 `US_FUTURES` 行情） | 订阅后重跑 `uv run pytest -m webull_live -s` |
| 期货代码格式 | 一位年份短代码，如 `ESZ6`（SDK 文档示例是 `ESZ3`） | `contracts.short_symbol` |
| 返回结构 | 兼容 `[{symbol, result:[...]}]`、`{result:[...]}`、`{data:[...]}` 和裸列表 | `sources/webull.py` 的 `parse` |
| `time` 字段口径 | 按 K 线**起始**时刻处理 | `parse`；若实际是结束时刻，所有线会晚一格 |
| 是否返回夜盘 | 假定返回 | `collect.yaml` 的 `include_eth` |
| 单次回看上限 | 按 SDK 文档的 1200 根 | `collect.yaml` 的 `max_bars_per_request` |

核实方法：在项目根 `.env` 放好凭证后运行 `uv run pytest -m webull_live -s`。

CSV 布局与列：

```
<data_root>/webull/ES/ESZ2026/2026-11-23.csv
instrument_root,contract_id,timeframe,bar_start_utc,open,high,low,close,volume,source,bar_end_utc,fetched_at_utc
```

采集规则：

- **只存已收线的**：`bar_end_utc` 晚于取数时刻的线不保存。
- **只追加**：已有的行不改写、不删除。同一根线再次取到且内容不同，追加一行带新取数时刻的记录，入库后成为新 revision。
- **补缺**：每次请求回看窗口（默认 2 个交易日）内的全部 K 线，把没保存过的补上。补回的线，可用时点是补回的时刻。
- **超出回看范围的缺失**补不回来，保留为缺口。
- **实际合约**：按换月规则确定请求哪个合约。返回的合约与预期不符时，本次不写任何行。不使用 ETF 或其他品种代替。
- **失败不造数**：请求失败、凭证缺失、没有行情权限、返回为空时不写任何行，写一条 `alerts`（`COLLECT_FAILED`），退出码非零。
- **夜盘**：18:00 ET 之后的线写入下一交易日的文件。17:00–18:00 ET 维护时段的线丢弃。

只读与凭证：

- 适配器只导入 `webull.core.client` 和 `webull.data.data_client`，只调用上面那一个行情接口。测试静态断言源码里没有交易模块和下单、改单、撤单调用。
- 凭证读自环境变量 `WEBULL_APP_KEY`、`WEBULL_APP_SECRET`、`WEBULL_REGION`（默认 `us`），其次项目根 `.env`。`.env` 不进 Git。
- SDK 默认把每次请求（请求头含 app key）打到标准输出和 `./webull_data_sdk.log`。适配器在创建客户端前关掉这两处输出。

已知限制：Webull 只采 5 分钟线，没有 1 分钟细节，阶段 3 在这些时段的成交时序歧义会比 Databento 时段多。

## revision 与可用时点

- 同键同内容的重复记录跳过。
- 同键不同内容：写入 `revision+1`，旧版本保留，各有自己的 `available_at_utc`。
- 没有取数时刻的来源，同一文件内同键出现两条不同内容，记 `CONFLICTING_DUPLICATE`，两条都不入库。
- 带取数时刻的 CSV，同键的多行按取数时刻先后成为依次的 revision，不算冲突。
- 文件的导入时刻不会被当作 `available_at_utc`。

## 增量导入

表 `ingest_files` 记录每个文件的路径、大小、SHA-256、已处理到的行数。

- DBN 文件：摘要相同则跳过。
- CSV 文件：只在末尾追加时，从上次处理到的行之后续读；前面的内容被改过时整文件重读，靠行级去重保证结果正确。

同样的输入重复运行，库中 K 线的数量和内容不变。

## 多来源取舍

同一品种同一交易日，按 `source_priority`（默认 `databento > webull`）在"有该日实际合约 RTH 5 分钟线"的来源里选一个，整日使用，不在日内混用。所选来源写入 `trading_days.source` 和快照。`scan` 按决策时点已可见的数据重新做这个选择。

多个来源重叠的交易日，可得性报告给出比对的 K 线数和开高低收不一致的条数。

## 可选的开发可见截止日

`dev_visible_through`（或 `ingest --through`）设了以后，交易日晚于它的真实 K 线不进运行库，只在 `withheld_coverage` 记每日条数和 RTH 内有数据的 5 分钟格数，没有价格。合成样本不受限制。

没设时全部导入，导入摘要和报告标 `HOLDOUT_BOUNDARY_NOT_SET`。阶段 5 分配盲测胶囊之前必须定下日期清单。

### 实时行情放行（2026-11-23 用户决定）

`config/live_data.yaml` 登记的来源（现在只有 `webull`）、从登记首日（ES、NQ 都是 2026-11-24）起的交易日，即使晚于截止日也进运行库。这些 K 线只供常驻纸面运行、影子和发布后监控使用：

- 切分登记把放行日排除在开发段、向前验证段、胶囊和余量之外：作者看得到它们，不能当封存数据。
- 聚合（`evolve`）照旧按截止日排除策略轨的这些 session（`AFTER_DEV_CUTOFF`）；评测只用封存数据。
- 同一天其他来源的数据（如 Databento 历史文件）照旧封存，也不会进以后的胶囊。
- 首日必须晚于任何已登记封存的交易日；冲突时这个品种不放行，导入摘要里写明冲突。现在 ES 已登记封存到 2026-11-23，NQ 没有登记。
- 截止日清理（重新导入、`--rebuild`）不会移出放行的实时行情。

导入摘要里的 `live_after_cutoff` 是这次放行的条数。

### 盘中主来源 tvfeed（2026-11-24 用户决定）

盘中行情以本地 TradingView 线为主，Webull 为备用。本地线是 `~/trading-desk` 的 price-feed 流水线每 5 分钟从 TradingView 读一次、存在本地的 5 分钟线（`state/price-feed/<美东日期>/`，保留 10 天）。

- **只读**：只读本地 JSON，不连 TradingView、Webull 或任何行情 API，不往那个目录写东西。`collect --from-tvfeed` 把它写成数据合同 CSV（`<data_root>/tvfeed/<根代码>/<合约>/<交易日>.csv`，来源 `tvfeed`，取数时刻用文件里的 `fetched_at`），再导入。
- **品种**：`ES1!`、`NQ1!`，CME 标准合约的 TradingView 连续合约（2026-11-23 起 price-feed 从 `MES1!`／`MNQ1!` 改为它们，只读这两个，旧的微型线即使还在目录里也不读）。按用户决定当作 ES／NQ 的行情：合约标识按 roll-v2 记实际合约，来源质量不降级。
- **优先级**：`source_priority` 是 `databento, tvfeed, webull`。引擎每天只按优先级选一个来源，一旦当天本地线有了 RTH 线就一直用它（阶段 3 冻结的行为）。
- **Webull 什么时候用**：盘中脚本只在本地线当天没有任何 RTH 线时（price-feed 没跑，或到期周）才起 `claude -p` 会话取 Webull（`collect --need-webull` 判断；09:30 那一轮第一根 RTH 线还没收线，不算缺线，不取）；那天节拍记 WARN `BACKUP_SOURCE_IN_USE`。本地线**午盘中途**断了不会切到 Webull，照样报断流。17:12 的 Webull 每日全量采集照常，作为当天的完整记录。
- **到期周不用本地线**：TradingView 的连续合约到期日前后才切月，而 roll-v2 在到期周的星期一就切到下一季；到期周里两边可能不是同一个合约，这几天的本地线一律不写，由 Webull 实际合约接管。
- **封存期不读**：只读实时行情放行首日（2026-11-24）及之后的日期目录，只写放行日的 K 线。
- **只用已收线的**：收线时刻晚于文件取数时刻的那根不写。盘中脚本先等 price-feed 这一轮的收据（`round-<HHMM>.json`，每 2 秒查一次，最多 90 秒，整点 180 秒），等不到就用上一轮的数据并记日志。

## 派生数据与因果可见性

### 5 分钟线

- 来源的原始层是 1 分钟线时，按纽约时间整 5 分钟聚合：开取第一根的开，收取最后一根的收，高低取极值，量求和。
- 按实际合约和时段切分。09:25 的线全是 ETH，09:30 的线全是 RTH。
- 区间内至少有一根 1 分钟线就生成，记 `constituent_count`（1–5）；一根都没有就不生成，按缺口处理。不插值，不用前值填充。
- 来源原生就是 5 分钟线（Webull）时原样使用，不聚合。

### 1 小时线

- 整点对齐（09:00–10:00、10:00–11:00…），由同一来源、同一实际合约、同一交易日的 5 分钟线聚合。跨 09:30 的那根 `session_type` 为 `MIX`。
- 小时内 RTH 预期的 5 分钟线不全时不生成，并为当日实际合约记一条 `INCOMPLETE_HOUR`。夜盘部分不要求每格都有。
- 只有 `bar_end_utc` 和 `available_at_utc` 都不晚于决策时点的 1 小时线才算已完结。

### `known_at_utc`

派生线的 `available_at_utc` 就是它的 `known_at_utc`：

```
known_at = max( 桶结束时刻 + (有估计值依赖时加估计延迟), 各依赖第一个 revision 的 available_at )
```

依赖之后出现新 revision 时，派生线也产生新 revision，可用时点是那个依赖 revision 的 `available_at_utc`，旧版本保留。所以在修订之前的时点查询，拿到的仍是旧版本。

### RTH 日高低差（供 ADR）

`context.rth_day_ranges_asof(con, settings, root, as_of, n=10)` 返回当前交易日之前的 n 个 RTH 交易日，只用 `as_of` 时可见的 5 分钟线。当前未结束的交易日不在其中。每项带 `complete`；有非预期缺口或没有数据的交易日照样列出并给原因，由使用方数有效日。

表 `rth_days` 是最新视图的物化，供报告使用，含 `known_at_utc` 和依赖清单。

### 参考价位

`context.reference_levels_asof(...)` 给出 PDH、PDL、上一 RTH 收盘、ONH、ONL，全部取自当日实际合约：

- 前一交易日的数据必须是同一合约的完整 RTH。换月后新合约在前一日没有数据时，标 `UNAVAILABLE:CONTRACT_ROLLED_NO_PRIOR_DATA`；前一日有缺口时标 `UNAVAILABLE:PREV_DAY_INCOMPLETE`。
- ONH、ONL 取当日 RTH 开盘前、决策时点已可见的 ETH 5 分钟线。

表 `reference_levels` 按"当日 RTH 开盘"这个时点物化。注意：09:25–09:30 那根线要到 09:30 之后才可见，所以物化值不含它；盘中用 as-of 函数查询会包含。

### 查询接口

`context.visible_bars(con, source, contract_id, timeframe, as_of_utc, ...)`：只返回 `bar_end_utc <= as_of` 且 `available_at_utc <= as_of` 的线，每根取当时可见的最高 revision。

### 可重现

派生线先删后建，结果只取决于原始数据和配置。清空派生结果后重建，逐条一致（有测试）。

## 校验规则与异常类别

异常记在表 `data_anomalies`，带类别、来源、合约、周期、时间范围和说明。

| 类别 | 何时产生 | 处理 |
| --- | --- | --- |
| `OFF_TICK_PRICE` | 价格不是最小变动价位的整数倍 | 该行不入库 |
| `OHLC_INCONSISTENT` | 高低与开收矛盾，或成交量为负 | 该行不入库 |
| `OUTSIDE_SESSION` | 起始时刻在每日维护时段（17:00–18:00 ET）、周末，或超出日历范围 | 该行不入库 |
| `CONFLICTING_DUPLICATE` | 无取数时刻的来源，同一文件内同键内容不同 | 都不入库；`scan` 报 `DATA_ERROR` |
| `UNMAPPED_INSTRUMENT` | DBN 记录不在符号映射区间内 | 该行不入库 |
| `BAD_ROW` | CSV 行无法解析（未登记品种、周期不支持、时刻未对齐等） | 该行不入库 |
| `UNEXPECTED_GAP` | RTH 内预期的 5 分钟线不存在 | 该交易日不算完整有效日；`scan` 报 `DATA_ERROR` |
| `WRONG_CONTRACT` | 换月规则指定的实际合约没有数据，但同品种其他合约有 | 同上 |
| `THIN_BAR` | RTH 内派生 5 分钟线的 `constituent_count` 小于 3 | 仅提示 |
| `INCOMPLETE_HOUR` | 小时内 RTH 预期的 5 分钟线不全 | 不生成该 1 小时线 |

`scan` 另有三种只在扫描结果里出现的问题：`NO_DATA`（该时段没有可见数据）、`LATEST_BAR_MISSING`（最近一根已收线的 K 线不存在）、`STALE_DATA`（最新 K 线落后两根以上）。

### 交易日状态

表 `trading_days` 每个品种每个交易日一行：实际合约、换月规则版本、所选来源、RTH 预期与实有根数、缺口段数、是否降级、是否提前收盘、是否完整有效。

- **预期的 K 线**只针对 RTH。夜盘成交稀疏，不做缺口判定。
- **提前收盘**之后没有 RTH K 线不算缺口。
- **进行中的交易日**：最后一根之后的缺失，如果该来源在当日 RTH 收盘后没有任何数据，视为当日尚未结束（`DAY_IN_PROGRESS`），不登记缺口；之后有了数据再重算。
- **降级日**不算缺口，但不计入可封存胶囊的有效日。

### 数据阻止状态

`scan` 发现问题时把该 scope 标为不可新入场（表 `scope_data_status`，按 scope 和风险域区分），同一事务里写 `freeze_events`（原因 `DATA`）和 `alerts`；数据恢复后解除，异常记录保留。更新带 `version` 校验。

### 快照

`market_snapshots` 每行有唯一 ID 和内容摘要。摘要覆盖 scope、决策时点、交易日、实际合约、所选来源、换月规则版本、日历版本，以及每根 K 线的起始时刻、revision、开高低收量和可用时点。相同输入得到相同摘要和相同 ID。

## 存储

| 库 | 位置 | 内容 |
| --- | --- | --- |
| 运行库 | `<data_root>/runtime/trading_agent.sqlite` | 全部业务表 |
| 发布库 | `releases/release-state.sqlite` | `active_pointers`（按四维 scope）和 `release_events`。阶段 1 只建表，不写 |

两个库都启用 WAL 和 `busy_timeout`。结构由编号 migration 管理（`src/trading_agent/migrations/`），已应用的编号记在 `schema_migrations`。运行库目前有 `0001_initial`（阶段 1）、`0002_engine`（阶段 3）和 `0003_manual`（阶段 4）。带状态的表有 `version` 列，用 `db.versioned_update` 做乐观锁更新；状态和对应事件在同一事务提交。

纸面、人工、影子和回放的记录用 `risk_domain`、`run_mode` 和 `run_id` 区分。

| 表 | 用途 | 由哪个阶段填充 |
| --- | --- | --- |
| `ingest_runs`、`ingest_files` | 导入运行与文件登记（提示词包未列出，为幂等所需的补充） | 1 |
| `bars` | 原始与派生 K 线 | 1 |
| `day_quality` | 来源的逐日质量状态 | 1 |
| `withheld_coverage` | 截止日之后的覆盖计数（提示词包未列出的补充） | 1 |
| `data_anomalies` | 校验异常 | 1 |
| `trading_days` | 交易日状态 | 1 |
| `rth_days` | RTH 日高低差 | 1 |
| `reference_levels` | 参考价位 | 1 |
| `market_snapshots` | 快照 | 1 |
| `scope_data_status` | scope 的数据阻止状态 | 1 |
| `scan_events` | 每轮扫描（含 `strategy_family`、`strategy_state_json`） | 1（数据校验），4（策略） |
| `alerts` | 告警 | 1 起 |
| `freeze_events` | 冻结与解除 | 1（数据原因），9（其余） |
| `paper_orders`、`paper_fills` | 纸面订单与成交。阶段 3 给订单加了激活时刻、过期时刻、信号 K 时刻、触发价、限价、止损、目标描述、手数、标记、结束原因 | 3 |
| `engine_runs` | 引擎的每个运行：scope、模式、风险域、三个水位、当前策略状态 | 3 |
| `paper_positions` | 纸面仓位：成交价、止损、目标、截止强平时刻、标记、绑定的版本元组 | 3 |
| `paper_trades` | 已完结的纸面交易：盈亏、费用、两种 R、退出原因、标记 | 3 |
| `explanation_requests` | 给模型的解释请求队列，带快照 ID 和尝试次数 | 3 起 |
| `account_risk_days` | 真实账户按交易日的已实现亏损、预留、止损次数、是否停手 | 3（结构），4（使用） |
| `account_risk_events` | 真实账户已完结交易的登记，按券商 ID 去重 | 3（结构），4（使用） |
| `risk_reservations` | 人工计划的风险预留 | 3（结构），4（使用） |
| `trader_plans` | 人工交易计划。阶段 4 加了 scope、`setup_id`、激活时刻、已成交手数、预检时的版本元组 | 4 |
| `plan_events` | 计划状态变化的审计：前后状态、时刻、操作者 | 4 |
| `manual_trades` | 人工成交。一行是一笔成交的开仓部分或平仓部分；阶段 4 加了去重键、开平、交易日、是否隔离、去重规则、关联建议与说明、平均入场价、已实现盈亏、退出类型、`plan_adherence`、`strategy_alignment` | 4 |
| `manual_order_events` | 订单生命周期事件。阶段 4 加了合约、方向、订单类型、价格、数量 | 4 |
| `broker_snapshots` | 券商只读快照 | 4 |
| `discipline_alerts` | `watch` 的纪律提醒。阶段 4 加了交易日、持仓键、级别、文字、解除时刻 | 4 |
| `discipline_events` | 纪律事件：类型、动作时点、关联的计划和成交、`plan_adherence`、`strategy_alignment`、证据 | 4 |
| `watch_state` | `watch` 的状态：心跳、上一快照的持仓和止损、首次看到无止损的时刻 | 4 |
| `trader_notes` | 交易员纠正 | 4 |
| `sessions` | 交易 session 与 KPI | 4、6A |
| `candidates`、`candidate_ledger` | 候选与台账 | 6A、6B |
| `memory_items` | Memory 条目 | 7 |
| `evaluation_runs` | 评测运行 | 8 |
| `dead_letter_queue` | 失败任务死信 | 10 |

阶段 2–10 的表只建了最小可用的列。阶段 1–3 验收前契约未冻结，可以用新的编号 migration 调整。

## 假设

以下是规格没有定死、这里选了保守做法的地方。改动任何一条都应同步改这份文档。

1. **日历**：RTH 的开收和提前收盘取 `exchange_calendars` 的 `XNYS`，ETH 用固定的 Globex 时段（前一日 18:00 至当日 17:00 ET）。没有用 `CMES` 日历，因为它不区分 RTH。休市日的 Globex 交易归入下一交易日的 ETH 背景。
2. **历史数据的估计延迟**：5 秒。实验预设，可在积累 Webull 采集数据后校准。
3. **换月规则 `roll-v2`**：到期日前第 6 个日历日起换月。那天是星期六，所以实际从到期周的星期一交易日起，按交易日整日切换（含周日晚上开始的夜盘）。2026-11-22 由用户确定，取代 `roll-v1`（前第 8 个日历日，比成交量交叉日早 1–2 个交易日）。四个品种都用 `roll-v2`，但只有 ES 用真实数据核对过。改换月规则后要运行 `ingest --rebuild`，重算交易日状态和派生数据。
4. **稀疏分钟**：5 分钟区间内有至少一根 1 分钟线就生成 5 分钟线。
5. **每日单一来源**：见"多来源取舍"。
6. **Webull 接口细节**：见"Webull 来源"的未核实项目表。
7. **时刻精度**：库内时刻是整数秒。带小数秒的取数时刻向上取整，可用时点宁晚勿早。
8. **物化的参考价位**以 RTH 开盘为计算时点。
9. **可分配窗口的计数口径**：每个连续覆盖区段内，从预热充足的那天起数完整有效且非降级的 RTH 日，胶囊数为它除以 20 取整，确认段数为它除以 40 取整。开发期和验证窗另外扣除。
10. **scope 的周期**首版只接受 `5m`。


## Webull MCP 连接器（2026-11-23 起的每日来源）

Webull 的 OpenAPI 密钥没有期货行情，但 Claude 里的 Webull MCP 连接器有：`get_futures_bars` 能取 ES、NQ 的实时 5 分钟线（实测延迟 0 分钟）。连接器只能在 Claude 会话里调用，`collect` 程序自己调不了，所以分两步：

1. 会话里调用连接器，返回的 JSON 原样落到一个文件。返回很大（一天两个品种约 7 万字符），Claude Code 会自动把它存成文件，价格不经过对话内容。
2. `collect --from-mcp <文件>` 读这个文件。之后与其他来源完全一样：只存已收线的 K 线，只追加，内容变了记为修订，然后入库。

```bash
uv run python -m trading_agent collect --mcp-request          # 打印该请求的合约代码、起始时刻、根数
uv run python -m trading_agent collect --from-mcp <JSON 文件>   # 文件里有几个已登记的品种就存几个
```

口径（2026-11-23 实测核对）：

- 合约代码是短格式（`ESZ6`、`NQZ6`），入库时规范化为 `ESZ2026`、`NQZ2026`。只接受当日的实际合约，别的合约整份拒收。
- `time` 是 K 线的起始时刻，UTC。返回按时间倒序，保存时按升序。
- 取数时刻取文件的修改时间（可用 `--fetched-at` 指定）。它是真实到达时刻，`available_at_estimated` 为 `false`。起始时刻加 5 分钟晚于取数时刻的那根还没收线，不保存。
- 文件位置：`<data_root>/webull/<根代码>/<合约>/<交易日>.csv`，来源名 `webull`。
- 包含夜盘。一个完整交易日是 276 根（前一日 18:00 ET 至当日 17:00 ET）。

**与开发可见截止日的关系**：这些数据都晚于 2024-12-31。2026-11-23 及之前采集的价格不进运行库，只记覆盖和内容摘要（已登记为封存）；2026-11-24 起按实时行情放行进运行库（见上文"实时行情放行"）。2026-09-05 至 2026-11-19 之间没有数据（Databento 文件到 09-04 为止）。

### 每日定时（流水任务）

2026-11-23 经用户要求安装了系统定时任务 `com.wenye.trading-agent-collect`（`~/Library/LaunchAgents/`，源文件在 `ops/launchd/`）：周一到周五 17:12（纽约时间，CME 收盘后）运行 `ops/launchd/run-collect-mcp.sh`。

1. 不是交易日就安静退出。
2. 用 `claude -p` 无人值守地调起 Claude Code。允许它用的工具只有三个：工具检索、取期货 K 线的连接器、`collect --from-mcp` 命令。
3. 脚本自己用 `collect --check-day` 核对当天 ES、NQ 的文件是否覆盖了全部 RTH 5 分钟线，不依赖 Claude 的说法。
4. 核对不过就发 Telegram（`ops/notify-telegram.sh`），说明是 Claude Code 没跑成功（多半是登录或连接器授权失效）、连接器不可用，还是数据不完整。Telegram 发不出去时退回 macOS 本机通知。

日志在 `<data_root>/logs/collect-mcp/<交易日>.log`。Telegram 凭证复用 `~/trading-desk/.env` 里的机器人（用户同意），脚本只解析 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 两个键；也可以另放在 `~/trading-agent-data/secrets/telegram.env`。

```bash
./ops/launchd/run-collect-mcp.sh                # 手工跑一次（核对今天）
launchctl print gui/$(id -u)/com.wenye.trading-agent-collect | head
launchctl bootout gui/$(id -u)/com.wenye.trading-agent-collect      # 停用
```

**限制**：

- 提醒只在任务跑起来之后才会发。电脑关机时任务不会跑，也就不会提醒；睡眠中错过的那一次，launchd 会在唤醒后补跑。
- 漏掉的日子靠回看补：每次请求回看 2 个交易日，单次上限 1200 根（约 4 个交易日）。
- 采集时运行库被别的进程占用，入库那一步会失败，但 CSV 已经存好，下一次 `ingest` 会补上。
- 更早的历史补不回来：连接器只按当前合约代码取数，换月之前的合约要单独请求。
- NQ 只是存数据。NQ 的 scope 没有启用，也没有基线。
