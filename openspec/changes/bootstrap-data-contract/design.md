# Design

## Context

动机见 proposal.md。这里只列影响做法的现状和约束。

**仓库现状**：没有代码。本机 `python3` 是 3.14，没有 3.12；`uv` 可用。`data/` 当前未被 Git 忽略。

**历史数据（已实测）**：`data/GLBX-20260905-AKYFXCHGY4/glbx-mdp3-20190905-20260904.ohlcv-1m.dbn`，实施时移到项目外。

| 项目 | 实测值 |
| --- | --- |
| 格式 | DBN v3，未压缩，`ohlcv-1m`，`stype_in=parent`，`stype_out=instrument_id` |
| 范围 | 2019-09-05 00:00 UTC 至 2026-09-04 20:59 UTC |
| 记录数 | 3,945,840 条，140 个 instrument_id |
| 代码 | 172 个：40 个单腿（`ESH0`…`ESZ9`），132 个日历价差（如 `ESH0-ESM0`） |
| 代码复用 | 年份只有一位。`ESH0` 在 2019-09-05 至 2020-04-05 指 2020 年 3 月合约，2024-12-08 起指 2030 年 3 月合约 |
| 价格 | 定点整数，单位 1e-9；`ts_event` 是 1 分钟线的起始时刻 |
| 稀疏性 | 没有成交的分钟没有记录 |
| 质量 | `condition.json` 共 2231 天，2213 天 `available`，18 天 `degraded` |
| 缺失字段 | 没有 `available_at`，没有 revision |

**每日数据**：用户要求每 5 分钟从 Webull 读取并保存为 CSV。`~/trading-desk/pipelines/pa-report/pa_sources.py` 用 Webull OpenAPI SDK（`WEBULL_APP_KEY`、`WEBULL_APP_SECRET`）取 5 分钟线，但它对期货明确写着"没有实时期货行情"，改用 SPY 代理。本项目必须用实际 ES 合约，所以 Webull 能否给出 ES 期货 K 线是未核实的前提。

**历史与每日数据之间有空档**：Databento 文件止于 2026-09-04，今天是 2026-11-22。这两个半月的数据既不在文件里，也很可能超出 Webull 单次能回补的范围。

**提示词包的硬约束**：UTC 存储、纽约时间展示；价格内部用整数 tick；只读行情；同一输入重跑结果一致；每个输出能追溯到来源与 revision；阶段 1–3 验收后表结构进入 `FROZEN_CONTRACT`；保留集不放在项目目录内。

## Goals / Non-Goals

**Goals:**

- 历史文件一次导入；之后每 5 分钟一条 `collect` 命令取数、落盘、入库。
- 阶段 2–5 需要的行情输入（5 分钟线、1 小时线、ADR 日高低差、参考价位）在阶段 1 就有因果正确的来源。
- 表结构一次建全，避免在契约冻结后再补表。
- 真实行情和数据库都不在项目目录内。

**Non-Goals:**

- 不计算 EMA、ATR、swing 等策略特征（阶段 2、3）。
- 不封存盲测胶囊、不做 OS 身份隔离（阶段 5、8）。本阶段只登记路径并报告可分配数量。
- 不读取 Webull 的持仓、挂单、成交（阶段 4 的 `watch`）。
- 不安装定时任务。
- 不做连续合约价格调整（back-adjust）。成交和盈亏始终用实际合约。

## Decisions

### 1. 范围只到 Prompt 0 + Prompt 1，外加 Webull 采集

提示词包规定一次只实施一个点名阶段、每阶段一个 Git 分支。本变更在分支 `phase-1-data-contract` 上完成，不合并到 `main`，等用户确认。Webull 采集属于提示词包 Prompt 1 第 3 条允许的"常用行情源的只读适配器"，所以放在这里。

*备选*：把一期（阶段 1–5）放进一个变更。否决，因为阶段 2 的非 Spike 形态量化草案需要用户确认，会卡住整个变更。

### 2. 数据根目录在项目外

`config/paths.yaml` 的 `data_root` 默认 `~/trading-agent-data/`，可用环境变量 `TRADING_AGENT_DATA_ROOT` 覆盖。布局：

```
~/trading-agent-data/
  databento/GLBX-20260905-AKYFXCHGY4/     ← 从项目 data/ 移来
  webull/ES/<contract_id>/<交易日>.csv    ← collect 写入
  runtime/trading_agent.sqlite            ← 运行库
```

运行库含全部价格，所以也放在这里。发布库只有指针和审计事件，按提示词包留在 `releases/`。项目内的 `data/` 目录保留为空（只有说明文件），合成样本放在 `tests/fixtures/`。加载器拒绝位于项目目录内的 `data_root`。

保留集路径另行登记，与 `data_root` 分开；阶段 1 不往里写东西。

*备选*：留在项目内靠 `.gitignore` 排除。否决，用户已同意移出，且提示词包要求保留集不在作者可写工作区内，移出是往那个方向走的第一步。

### 3. 用 `databento-dbn` 本地解码历史文件

`databento-dbn` 只做解码，不带网络客户端，不需要 API key。流式迭代，分批写入 SQLite（每批约 5 万行）。符号映射取自 DBN 文件头。

*备选*：先转成 CSV。否决，多一步人工操作。

### 4. Webull 采集：一次运行做一件事

`collect` 每次运行：按换月规则确定当前实际合约 → 向 Webull 请求该合约最近 N 根 5 分钟线（默认覆盖最近 2 个交易日）→ 丢掉取数时刻尚未收线的那一根 → 把库里和 CSV 里还没有的行追加到当日 CSV → 触发增量导入。

- **只取已收线**：`bar_end_utc` 晚于取数时刻的线一律不落盘，避免把形成中的线当成定稿。
- **真实的可用时点**：CSV 每行带 `fetched_at_utc`。入库时 `available_at_utc` 取该行首次被取到的时刻，`available_at_estimated=false`。停机后补回的线，可用时点就是补回的时刻，不倒填。
- **补缺**：每次都请求一段回看窗口而不是只取最后一根，所以漏跑几次后下一次运行自动补齐。超出 Webull 回看范围的缺口补不回来，留作缺口。
- **修订**：同一根线再次取到且内容不同，在 CSV 追加一行新的（带新的 `fetched_at_utc`），入库时成为 `revision+1`。CSV 只追加、不改写。
- **含 ETH**：取数包括夜盘，因为 ONH、ONL 和 1 小时线需要它。
- **失败不造数**：请求失败、返回空、或返回的合约与预期不符时，本次不写任何行，记一条告警，退出码非零。

调度方式是每 5 分钟、在收线后约 10 秒运行一次。本变更只提供 launchd 样例文件 `ops/launchd/collect.plist.example` 和等价的手工命令，不安装。

*备选*：常驻进程自己计时。否决，一次性命令加外部调度更容易做到"同一输入重跑不重复"，也和阶段 10 的 launchd 方案一致。

*备选*：通过 Claude 会话里的 Webull 连接器取数。否决，定时任务不应依赖一个模型会话在线。

### 5. Webull 只读保证与凭证

- 适配器只导入 SDK 的行情模块；测试静态断言 `src/trading_agent/` 中不出现 SDK 的交易模块，也不出现下单、改单、撤单的调用名。
- `WEBULL_APP_KEY`、`WEBULL_APP_SECRET`、`WEBULL_REGION` 从环境变量或项目根 `.env` 读取。`.env` 不进 Git，`.claude/settings.json` 拒绝读取它。
- SDK 默认会把请求（含 app key）打印到标准输出并写本地日志文件（`trading-desk` 的代码注释记录了这一点）。适配器关闭这两处输出，测试断言采集后的日志和标准输出里不含 key。
- Webull 的期货代码格式、所需行情订阅和回看上限以实测为准，写入 `docs/data-contract.md`。

### 6. 两种来源的原始周期不同，各自原样存

| 来源 | 原始层周期 | 5 分钟线 |
| --- | --- | --- |
| Databento | 1 分钟 | 由 1 分钟线聚合 |
| Webull | 5 分钟 | 原生 |

Databento 的 1 分钟线按纽约时间整 5 分钟边界聚合，按实际合约和交易时段切分。一个 5 分钟桶内至少有一条 1 分钟线就生成并记录 `constituent_count`（1–5）；一条都没有就不生成，按缺口处理。不插值。Webull 原生 5 分钟线的 `constituent_count` 为空。

保留 Databento 的 1 分钟线还有一个用处：阶段 3 在只有 5 分钟 OHLC 时要标 `AMBIGUOUS_BAR`，有 1 分钟线可以把歧义缩小到 1 分钟内。Webull 时段没有这个条件，歧义照标。

*备选*：让 Webull 也采 1 分钟线。暂不做，用户要求的是每 5 分钟读一次；以后要加只需多一个周期配置。

### 7. 一个 scope 的一个交易日只用一个来源

同一交易日若两个来源都有数据，按 `config/paths.yaml` 里的 `source_priority`（默认 `databento` 在前）选一个，整日使用，不在日内混用两个来源的线。所选来源写入快照。两个来源重叠的日期，可得性报告给出逐根差异的条数统计，供核对 Webull 与 Databento 是否一致。

*备选*：逐根取优先级高的。否决，日内混用会让 1 小时线和日高低差的依赖来源不一致，也难以重现。

### 8. 只导入单腿合约，实际合约 ID 用四位年份

价差合约（代码含 `-`）丢弃并计数。`contract_id` 规范化为 `<根代码><月份字母><四位年份>`，如 `ESH2020`。年份解析：取符号映射区间的起始日期，找出不早于它的、个位数与代码一致的第一个到期月。到期日取合约月的第三个星期五。映射区间之外的记录按 `UNMAPPED_INSTRUMENT` 拒收。Webull 返回的合约代码用同一套规范化。

### 9. 主力合约与换月规则版本化

`config/instruments.yaml` 登记 `roll_rule_version`。规则只决定"某交易日哪个合约是该 scope 的实际合约"，不改价格；`collect` 用它决定向 Webull 请求哪个合约。可得性报告并列给出每次换月前后两个合约的成交量交叉日。

- `roll-v1`（已停用，定义保留）：到期日（第三个星期五）之前第 8 个日历日（星期四）起换月。用真实数据核对后发现它比成交量交叉日早 1–2 个交易日。
- `roll-v2`（现用，2026-11-22 由用户确定）：到期日之前第 6 个日历日起换月。那天是星期六，所以实际从到期周的星期一交易日起，含周日晚上开始的夜盘。28 次换月中 17 次与成交量交叉日同一天（2022 年 6 月以来全部吻合），11 次比交叉日晚 1 个交易日（2019 年 9 月至 2022 年 3 月）。

改换月规则后运行 `ingest --rebuild`：清空派生数据和交易日状态，从原始 K 线重算。原始 K 线不动。

*备选*：按成交量交叉动态换月。否决作为默认，回放时有前视风险；留作报告里的对照。

### 10. 价格换算为整数 tick

Databento：`price_ticks = raw_price / (tick_size × 1e9)`。Webull 和 CSV：十进制价格用 `Decimal` 除以 tick。不能整除的记 `OFF_TICK_PRICE`，不四舍五入。

### 11. 历史数据的 `available_at` 用显式估计

Databento 文件没有到达时刻：`available_at_utc = bar_end_utc + 固定延迟`（默认 5 秒，实验预设），`available_at_estimated=true`。派生线的 `known_at_utc` 取全部依赖线里最晚的 `available_at_utc`。Webull 采集的线用真实取数时刻（决策 4）。

### 12. revision 由内容变化产生

主键为 `(source, contract_id, timeframe, bar_start_utc, revision)`。同键同内容跳过；同键不同内容写 `revision+1`，保留旧版本和各自的首次可见时刻。Databento 文件内同键出现两条不同内容属于冲突，记 `CONFLICTING_DUPLICATE`，该 bar 不入库。Webull CSV 里同键的多行按 `fetched_at_utc` 先后视为依次的修订，不算冲突。

### 13. 增量导入

`ingest` 递归扫描 `data_root`。`ingest_files` 表（提示词包未列出，为幂等所需的补充）记录路径、大小、SHA-256、已处理到的行数。

- DBN 文件写一次就不变：摘要相同直接跳过。
- Webull 的当日 CSV 会不断追加：文件变长时从上次处理到的行之后继续读；若前缀内容变了（不该发生），整文件重读，靠决策 12 的行级去重保证结果正确。

非 `ohlcv-1m` 的 DBN 文件报告为 `UNSUPPORTED_SCHEMA`，不导入、不中断。同目录的 `condition.json` 提供逐日质量状态。

### 14. 交易日历与时段

RTH 的开收和提前收盘取 `exchange_calendars` 的 `XNYS` 日历，版本锁定并写入快照。RTH 为纽约时间 09:30–16:00；ETH 为同一 Globex 交易日内 RTH 之外的时段，交易日从前一日 18:00 ET 开始，17:00–18:00 ET 是维护时段。存储一律 UTC。

*实施时的调整*：原计划用 CME 股指期货日历（`CMES`），但它不区分 RTH，也不含美股的提前收盘，所以改用 `XNYS` 加固定的 Globex 时段。休市日的 Globex 交易（没有 RTH）归入下一交易日的 ETH 背景。

### 15. 两个 SQLite 库与 migration

运行库在 `data_root/runtime/`，发布库在 `releases/release-state.sqlite`，都启用 WAL 和 `busy_timeout`。表结构用编号 migration 管理。阶段 1 只真正读写行情相关的表，其余表建好结构并在 `docs/data-contract.md` 标注由哪个阶段填充。

### 16. 快照

`market_snapshots` 的每个快照有唯一 ID 和内容摘要，摘要覆盖 scope、`as_of_utc`、所选来源、所含 bar 的键与 revision、换月规则版本、日历版本。

### 17. 开发可见截止日改为可选

数据移出项目后，不再强制要求先给截止日才能导入。`dev_visible_through` 留作可选配置：设了就只把不晚于它的 K 线写进运行库，报告对之后的区段只报覆盖不报价格；没设就全部导入，报告顶部标 `HOLDOUT_BOUNDARY_NOT_SET`。

理由：阶段 1 只做导入、校验和覆盖统计，不看任何策略结果，不会污染以后的盲测数据。真正要划线的时刻是阶段 5 冻结基线、分配胶囊之前，那时必须先定下日期清单。

### 18. 实时来源暂缓（2026-11-22 实施中的决定）

用真实凭证实测：凭证有效（股票快照返回 200），但期货快照和期货 K 线都返回 `MARKET_DATA_NOT_SUBSCRIBED`。用户确认 Webull 账号没有期货行情。用户的决定：

- **先不接实时来源。** 阶段 2–5（策略、引擎、回放、基线）只需要历史数据，现有 Databento 数据够用。来源等做到盘中 `scan`、`watch` 时再定。
- **Webull 采集代码保留并标为不可用。** 采集逻辑与来源无关，换来源只需换适配器。`live-bar-collection` 规格描述的行为对将来的任何来源都适用，其中写明 Webull 的几处到时随来源一起修订。

因此 tasks.md 里依赖真实期货 K 线的三项（7.2、12.3、12.6）改写为当前能验证的内容，原文保留在任务后面的说明里。

## Risks / Trade-offs

- **Webull 可能取不到 ES 期货 K 线**（账号没有期货行情订阅，或 OpenAPI 不提供）→ tasks.md 在写采集代码前先做一次实测；取不到就停下来报告，不退回到 SPY 代理，因为提示词包要求信号和成交用实际 ES 合约。
- **2026-09-05 至今的空档补不回来** → `collect` 首次运行尽量回补 Webull 能给的最早数据，剩余空档在可得性报告里明确列出。ADR 和 1 小时线的预热要在空档之后重新累计；若要连续数据，需要用户再从 Databento 下载这一段放进数据根目录，导入流程不用改。
- **两个来源的线可能不完全一致**（成交口径、时间戳对齐）→ 日内不混用（决策 7），重叠日报告差异统计；差异明显时由用户决定优先级。
- **Webull 采集的线没有 1 分钟细节** → 阶段 3 的成交歧义标记在 Webull 时段会更多，这是已知代价。
- **全量历史对开发身份可读** → 移出项目只是第一步，同一系统用户仍能读到。阶段 5 分配胶囊前要定下日期清单，阶段 8 再做 OS 身份隔离。阶段 1 的报告在未设截止日时标 `HOLDOUT_BOUNDARY_NOT_SET` 作为提醒。
- **历史数据的 `available_at` 是估计值** → 带标记，报告单列估计占比。
- **换月规则只在 ES 上核对过** → `roll-v2` 对 NQ、MES、MNQ 同样生效，但没有它们的数据可核对；启用这些品种前要各自核对。
- **18 个降级日** → 不计入可封存胶囊的有效日，报告逐日列出。
- **定时任务休眠或中断** → 回看窗口补缺；超过新鲜度阈值时 `scan` 给 `DATA_ERROR`，阻止新入场。
- **凭证泄露到日志** → 关闭 SDK 自带输出，测试断言。

## Migration Plan

1. 建好 `~/trading-agent-data/databento/`，把 `data/GLBX-20260905-AKYFXCHGY4/` 整个目录移过去。
2. 核对移动后 DBN 文件的 SHA-256 与 `manifest.json` 记载的一致。
3. 项目内 `data/` 只留说明文件。

回滚：把目录移回原处，删除分支 `phase-1-data-contract`。`~/Downloads/GLBX-20260905-AKYFXCHGY4.zip` 不动，可作备份。

## Open Questions

- Webull 期货 K 线的代码格式、单次回看上限、是否需要额外订阅，实测后写入文档。这些只影响适配器内部和回看窗口的默认值。
- 历史数据的估计延迟默认 5 秒，是否贴近 Webull 的实际到达延迟，可在积累几天采集数据后校准。
