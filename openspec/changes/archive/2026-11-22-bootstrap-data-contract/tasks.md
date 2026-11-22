# Tasks

## 1. 分支、环境与骨架

- [x] 1.1 从 `main` 新建分支 `phase-1-data-contract`；验证：`git branch --show-current` 输出该分支名
- [x] 1.2 新增 `.gitignore`，排除 `data/*`（保留 `data/README.md`）、`*.sqlite*`、`.env`、`secrets/`、`.venv/`、`__pycache__/`、`.DS_Store`、Webull SDK 的本地日志文件；验证：`git status --short` 不列出 `data/` 下的 DBN 文件
- [x] 1.3 新增 `pyproject.toml`：`requires-python = ">=3.12,<3.13"`，包名 `trading_agent`（`src/` 布局），依赖 `databento-dbn`、`webull-openapi-python-sdk`、`exchange_calendars`、`PyYAML`，开发依赖 `pytest`，全部锁定版本并生成 `uv.lock`；注册 pytest 标记 `realdata` 和 `webull_live`（默认都不运行）；验证：`uv sync` 成功，`uv run python -c "import trading_agent, databento_dbn, exchange_calendars"` 无报错
- [x] 1.4 建立目录 `config/`、`src/trading_agent/strategies/`、`tests/vectors/strategy/`、`tests/vectors/fills/`、`tests/fixtures/`、`reports/`、`prompts/`、`schemas/`、`candidates/`、`incidents/`、`memory/`、`releases/`、`docs/`、`ops/launchd/`，空目录放 `.gitkeep`；验证：`git ls-files` 列出每个目录下的占位文件

## 2. 把行情数据移出项目

- [x] 2.1 创建 `~/trading-agent-data/databento/`、`~/trading-agent-data/webull/`、`~/trading-agent-data/runtime/`；验证：三个目录存在
- [x] 2.2 把 `data/GLBX-20260905-AKYFXCHGY4/` 整个目录移到 `~/trading-agent-data/databento/`；验证：移动后 DBN 文件的 SHA-256 等于 `manifest.json` 记载的 `67a9f87f…bae8`，项目内 `data/` 下不再有该目录
- [x] 2.3 在项目 `data/` 写 `README.md`，说明真实行情在项目外的数据根目录及其布局；验证：文件存在并被 Git 跟踪

## 3. 项目指令与术语

- [x] 3.1 把提示词包复制为仓库根目录的 `trading_agent_v2_6_pa.md`；验证：与 `~/Downloads/trading_agent_claude_v2_6_pa.md` 的 SHA-256 相同
- [x] 3.2 把 Prompt 0 的正文写入根目录 `CLAUDE.md`，顶部加 `@docs/glossary.md` 引用，并补一句"行情来源为 Databento 历史文件和 Webull 采集，数据根目录在项目外"；验证：文件含"工作方式""契约冻结与并发规范""权限""统一口径""阶段完成判定"五节
- [x] 3.3 写 `docs/glossary.md`：四维 scope、运行模式、版本元组、三组状态枚举、RTH／ETH 与交易日定义、整数 tick、`available_at`／`known_at`／`revision`、`FIXTURE`、数据根目录；验证：`CLAUDE.md` 统一口径一节里的每个术语在此都有条目
- [x] 3.4 新增 `.claude/settings.json`，写入提示词包给出的 deny 规则，路径换成本机实际路径；验证：文件是合法 JSON，deny 列表含保留集路径、`releases/active*`、`releases/release-state.sqlite*`、`.env`、`secrets/`

## 4. 配置

- [x] 4.1 写 `config/instruments.yaml`：ES $50／点、NQ $20／点、MES $5／点、MNQ $2／点，最小变动 0.25 点，季月代码，`roll_rule_version: roll-v1`（到期日前第 8 个日历日的 RTH 开盘换月）；加载器拒绝未知品种；验证：单元测试断言四个品种的乘数、tick 价值，以及 `roll-v1` 对 2025 年四个季月给出的换月日
- [x] 4.2 写 `config/paths.yaml`：`data_root`（默认 `~/trading-agent-data`，可由 `TRADING_AGENT_DATA_ROOT` 覆盖）、发布库 `releases/release-state.sqlite`、项目外的保留集路径（仅登记）、历史数据的估计延迟（默认 5 秒）、`source_priority: [databento, webull]`、可选的 `dev_visible_through`；验证：单元测试断言 `data_root` 或保留集路径落在项目目录内时加载器报错，环境变量能覆盖默认值
- [x] 4.3 写 `config/watch.yaml`：`poll_interval_seconds: 60`、`presets: [60, 300]`、`stream_first: true`，加载器接受任意正整数秒、拒绝 0 和负数；验证：对应单元测试通过
- [x] 4.4 写 `config/collect.yaml`：采集周期 `5m`、回看窗口（默认 2 个交易日）、收线后等待秒数（默认 10）、是否含 ETH（默认是）；验证：加载器单元测试覆盖默认值和非法值
- [x] 4.5 实现交易日历封装：基于锁定版本的 `exchange_calendars`，给出每个交易日的 RTH 起止（含提前收盘）和 ETH 起止，并暴露日历版本号；验证：单元测试覆盖一个普通日、一个半日市、夏令时开始周和结束周、一个假日

## 5. 存储

- [x] 5.1 写 migration `0001_initial.sql`（运行库）：K 线表（主键含 `source, contract_id, timeframe, bar_start_utc, revision`）、`ingest_files`、派生结果表及 runtime-storage 规格列出的全部业务表，`scan_events` 含 `strategy_family` 与 `strategy_state_json`，带状态的表含 `version`；验证：测试在空库上应用后断言每张表存在
- [x] 5.2 写发布库 migration：`active_pointers`（四维 scope 唯一）和 `release_events`；验证：测试断言运行库没有 `active_pointers`、发布库没有业务表
- [x] 5.3 实现连接与 migration 执行器：运行库建在 `data_root/runtime/`，启用 WAL、`busy_timeout`，记录已应用编号，重复执行不改数据；提供带 `version` 校验的更新辅助函数；验证：测试覆盖重复初始化、读写并发不报锁错误、过期 `version` 的更新被拒、项目目录内没有运行库文件
- [x] 5.4 在 `docs/data-contract.md` 写"存储"一节：两库的位置与职责、每张表的用途与由哪个阶段填充、风险域与运行 ID 的隔离方式；验证：规格列出的每张表在文档里都有一行

## 6. 行情导入

- [x] 6.1 定义统一 K 线记录与价格换算（Databento 定点价格和十进制价格到整数 tick，不能整除记 `OFF_TICK_PRICE`）；验证：单元测试覆盖两种输入各自的整除与不整除情况
- [x] 6.2 实现合约解析：由符号映射区间把短代码解析成四位年份的 `contract_id`，丢弃含 `-` 的价差代码，映射区间外的记录记 `UNMAPPED_INSTRUMENT`；验证：单元测试断言 `ESH0` 在 2019-09-05 起的区间得到 `ESH2020`、在 2024-12-08 起的区间得到 `ESH2030`
- [x] 6.3 实现 Databento DBN 适配器：流式读取 `ohlcv-1m`（含 `.zst`），读文件头的符号映射，其他 schema 报 `UNSUPPORTED_SCHEMA`；读取同目录 `condition.json`；验证：用测试中生成的小型 DBN 样本断言记录数、合约、价格，以及 `trades` 文件被报告为不支持
- [x] 6.4 实现 CSV 适配器：校验必需列，缺列整体拒收；有 `fetched_at_utc` 列时取每根 K 线最早的取数时刻作 `available_at_utc`，同键多行按取数时刻先后作为依次的 revision；验证：单元测试覆盖合法文件、缺 `contract_id` 的文件、带取数时刻的文件、同键两行生成两个 revision
- [x] 6.5 实现入库：分批写入、同键同内容跳过、同键不同内容写 `revision+1` 并保留旧版本、Databento 文件内冲突记 `CONFLICTING_DUPLICATE`；没有取数时刻的来源按收盘加配置延迟估计并置 `available_at_estimated`；验证：测试覆盖重复导入记录数不变、修订生成新 revision、冲突两条都不入库
- [x] 6.6 实现数据根目录扫描与 `ingest_files`：递归扫描，摘要相同的文件跳过，被追加的 CSV 从上次行数之后续读，前缀变化时整文件重读；生成导入摘要（处理文件、跳过文件、价差丢弃数、各类异常计数）；验证：测试覆盖新增文件只读新文件、CSV 追加 3 行只新增 3 根、前缀被改后结果仍正确
- [x] 6.7 实现每日单一来源的选择：按 `source_priority` 为每个 scope 交易日选定来源，查询 5 分钟线时只返回所选来源的线；验证：测试覆盖两来源重叠日取优先来源、只有一个来源时用它、日内不出现两个来源的线
- [x] 6.8 实现可选的 `dev_visible_through`：设了就不入库更晚的 K 线，没设就全部导入并在摘要标 `HOLDOUT_BOUNDARY_NOT_SET`；验证：测试覆盖两种情况
- [x] 6.9 制作带 `FIXTURE` 标记的合成样本到 `tests/fixtures/`（CSV，另有脚本生成小型 DBN），覆盖正常读取、冲突、缺 bar、换月错误、夏令时转换、半日市、ETH→RTH 衔接；测试用临时目录作数据根目录；验证：导入后记录都带 `FIXTURE` 标记
- [x] 6.10 断言 `ingest` 只读：测试在导入前后比对来源文件摘要，并断言导入代码路径不引用任何网络库；验证：对应测试通过
- [x] 6.11 在 `docs/data-contract.md` 写"K 线合同""来源适配器""数据根目录与增量导入""多来源取舍""假设"几节，假设节记录：估计延迟 5 秒、`roll-v1`、稀疏分钟的处理、每日单一来源；验证：design.md 的每条决策在文档里都能找到对应说明

## 7. Webull 采集

- [x] 7.1 先实测前提：用 `.env` 里的凭证，对当前实际 ES 合约请求最近的 5 分钟线，记录可用的代码格式、返回字段、时间戳口径（起始还是结束时刻）、单次回看上限、是否返回夜盘；验证：把实测结果写进 `docs/data-contract.md` 的"Webull 来源"一节。**取不到 ES 期货 K 线时停止本组后续任务并向用户报告，不改用 SPY 等代理**（结果：2026-11-22 实测取不到，`MARKET_DATA_NOT_SUBSCRIBED`；已停下并报告，见 design.md 决策 18）
- [x] 7.2 实现 Webull 行情适配器：只导入 SDK 的行情模块，从环境变量或 `.env` 读凭证，关闭 SDK 的标准输出和本地日志，把返回解析成统一 K 线记录并规范化合约代码；验证：单元测试覆盖按 SDK 文档假定的返回形状、空返回、无权限错误、凭证缺失，并在文档中标明返回形状未经真实响应核实（原定用录制的真实响应测试；账号没有期货行情，无法录制，见 design.md 决策 18）
- [x] 7.3 实现采集逻辑：按 `roll-v1` 确定实际合约，请求回看窗口，丢弃取数时刻尚未收线的线，返回合约与预期不符时整次放弃；验证：用假适配器测试 09:35:10 运行只保留到 09:30 那根、返回别的合约时不写任何行且退出码非零
- [x] 7.4 实现 CSV 落盘：写到 `data_root/webull/<品种>/<contract_id>/<交易日>.csv`，只追加此前没有的行，内容变化的线追加新行并带新的 `fetched_at_utc`，不改写已有行；验证：测试覆盖重复运行不新增行、已有行逐字节不变、修订追加一行、夜盘的线归入正确的交易日文件
- [x] 7.5 实现补缺：恢复运行后回看窗口内缺失的已收线被补上，`available_at_utc` 为补回时的取数时刻；验证：用假适配器模拟漏跑三次后恢复，断言补齐且可用时点不倒填
- [x] 7.6 实现 `collect` 子命令：采集、落盘、触发增量导入，支持 `--scope`、`--json`，失败时不写数据、退出码非零并写一条 `alerts`；验证：测试断言成功后 `scan` 能看到新线，失败时 CSV 和库都不变
- [x] 7.7 只读与凭证断言：静态检查 `src/trading_agent/` 不引用 Webull 交易模块、不出现下单、改单、撤单调用名；采集测试断言标准输出、日志和 CSV 中不含凭证；验证：对应测试通过
- [x] 7.8 写 `ops/launchd/collect.plist.example`（每 5 分钟、收线后约 10 秒运行）和等价的手工命令，**不安装**；在 `README.md` 说明启用调度需要用户另行授权；验证：`plutil -lint` 通过，`launchctl list` 中没有新增本项目的任务
- [x] 7.9 在 `docs/data-contract.md` 补全"Webull 来源"：CSV 列、文件布局、补缺与修订规则、已知限制（无 1 分钟细节、回看上限）；验证：live-bar-collection 规格的每条要求在文档里都有对应说明

## 8. 派生背景

- [x] 8.1 实现 1 分钟线到 5 分钟线的聚合：按纽约时间整 5 分钟对齐，按实际合约和 RTH／ETH 切分，记录 `constituent_count`，空区间不生成；来源原生就是 5 分钟线时原样使用；验证：测试覆盖完整区间、3 根构成、空区间、09:25／09:30 时段边界、Webull 原生线不被聚合
- [x] 8.2 实现已完结 1 小时线：小时结束且数据可用后才可见，小时内有非预期缺口不生成；验证：测试覆盖 10:40 ET 查询只见到 09:00 那一根、缺 bar 的小时不生成
- [x] 8.3 实现 RTH 日高低差：只记录完整有效的 RTH 日，查询时排除当前交易日；验证：测试覆盖盘中查询不含当天、有缺口的交易日被排除并带原因
- [x] 8.4 实现参考价位 PDH、PDL、ONH、ONL、上一 RTH 收盘，写入 `reference_levels`，换月导致前一日属于另一合约时标为不可用；验证：测试覆盖普通日和换月日
- [x] 8.5 实现因果查询：每个派生结果记录依赖的 K 线键、revision 和 `known_at_utc`，按决策时点只返回当时可见的结果和 revision；验证：测试覆盖决策时点早于 `known_at`、迟到修订不影响更早时点的查询、补回的 Webull 线在补回之前不可见
- [x] 8.6 断言可重现：清空派生结果后重建，逐条比对一致；在 `docs/data-contract.md` 写"派生数据与因果可见性"一节；验证：重建测试通过，文档含 `known_at` 的计算规则

## 9. 校验与快照

- [x] 9.1 实现逐条校验：OHLC 一致性、重复与冲突、交易时段外、价格不在 tick 上，每个异常带类别、合约和时间范围；验证：每个类别各有一个触发和一个不触发的测试
- [x] 9.2 实现时间轴与缺口校验：按日历算出预期的 5 分钟线，区分预期空白与非预期缺口，对 RTH 内 `constituent_count` 偏低的线给 `THIN_BAR` 提示；验证：测试覆盖 RTH 中间缺一根、半日市收盘后不算缺口、夏令时切换周不误报
- [x] 9.3 实现换月一致性校验：按 `roll-v1` 确定 scope 当日的实际合约，其他合约的数据记 `WRONG_CONTRACT`；验证：换月错误的合成样本触发该异常
- [x] 9.4 实现降级日标记：读取来源的逐日质量状态；验证：测试用含 `degraded` 的 `condition.json` 断言该日被标记
- [x] 9.5 实现新鲜度检查与 scope 数据阻止状态：盘中最新线落后两根以上、非预期缺口、合约错误或冲突时标为不可新入场并记录原因，数据恢复后解除、缺口记录保留；验证：测试覆盖新鲜度不足、阻止、恢复三个过程
- [x] 9.6 实现快照封存：唯一 ID 加内容摘要，摘要覆盖所选来源、K 线键与 revision、换月规则版本、日历版本，写入 `market_snapshots`；验证：相同输入两次摘要相同，新增可见 revision 后摘要不同
- [x] 9.7 在 `docs/data-contract.md` 写"校验规则与异常类别"一节；验证：代码里定义的每个异常类别在文档里都有说明

## 10. CLI

- [x] 10.1 实现 `python -m trading_agent` 入口和 14 个子命令的解析，`--scope` 默认 `pa_brooks:ES:RTH:5m`，校验格式和品种是否登记；验证：测试覆盖 `--help` 列出全部子命令、默认 scope 回显、格式错误和未登记品种被拒
- [x] 10.2 未实现的 11 个子命令返回含 `NOT_IMPLEMENTED` 和所属阶段的结果，退出码非零，不触碰数据库；验证：参数化测试逐个调用并断言运行库文件摘要不变
- [x] 10.3 实现 `ingest`：串起目录扫描、导入、派生重建，支持 `--through`、`--json`；验证：对合成样本运行后断言 JSON 输出含运行 ID 和各项计数，退出码为 0，重跑结果一致
- [x] 10.4 实现最小 `scan`：按 `as_of_utc` 取可见数据、校验、封存快照，输出 JSON 和中文短报，`signal_status` 只取 `WAIT` 或 `DATA_ERROR`，写一条 `scan_events`，不写纸面订单；验证：测试覆盖数据正常得 `WAIT`、缺 bar 得 `DATA_ERROR`、`paper_orders` 始终为空、发布库内容不变
- [x] 10.5 更新 `README.md`：环境准备（`uv sync`、`.env` 里的 Webull 凭证）、数据根目录布局、`ingest`、`collect`、`scan` 的有效调用例；验证：按 README 的命令在合成样本上逐条执行成功

## 11. 数据可得性报告

- [x] 11.1 实现按实际合约的覆盖汇总：起止、RTH／ETH 有效日、非预期缺口、降级日清单、`available_at` 估计占比、revision 数量；验证：对合成样本生成的数字与手算一致
- [x] 11.2 实现按来源汇总与衔接：各来源覆盖范围、无来源覆盖的日期区段、重叠交易日数与不一致的 K 线条数；验证：测试用两个来源的合成样本断言空档起止和不一致条数
- [x] 11.3 实现换月核对：列出每次换月的规则日期、规则版本和前后合约的成交量交叉日及差值；验证：测试用构造的两个合约成交量断言交叉日
- [x] 11.4 实现预热覆盖：给出第一个满足 10 个完整 RTH 日和 10 根 1 小时线的交易日，空档之后重新计算；验证：测试断言数据开头和空档之后的交易日被标为预热不足
- [x] 11.5 实现窗口计数：可封存的 20 日胶囊数和连续 40 日确认段数，只计完整有效 RTH 日，不跨无来源覆盖的区段；验证：测试断言插入一个降级日或一段空档后计数按预期变化
- [x] 11.6 实现保留边界状态和 `NO_REAL_DATA` 标记：设了截止日时之后的区段只报覆盖，没设时顶部标 `HOLDOUT_BOUNDARY_NOT_SET`，只有合成样本时标 `NO_REAL_DATA` 且窗口计数为 0；验证：三种情况各一个测试
- [x] 11.7 把报告接到 `ingest` 结束时生成 `reports/data_availability.md`，并断言同样的库内容生成的报告除时间外一致；验证：可重现测试通过

## 12. 真实数据验收

- [x] 12.1 对 `~/trading-agent-data/` 运行 `ingest`，记录耗时和导入摘要；验证：摘要显示 132 个价差代码被丢弃，原始 DBN 文件的 SHA-256 在导入前后不变
- [x] 12.2 原样重跑 `ingest`；验证：文件被报告为已跳过，K 线记录数不变
- [x] 12.3 手工运行一次 `collect`，确认取不到数据时的行为；验证：输出 `MARKET_DATA_NOT_SUBSCRIBED`，没有生成任何 CSV，写了一条 `COLLECT_FAILED` 告警，退出码非零（原定验证成功采集；改为验证失败路径，见 design.md 决策 18）
- [x] 12.4 查看 `reports/data_availability.md`，核对：换月日与成交量交叉日的差异、18 个降级日是否都列出、2026-09-05 起的空档起止、胶囊与确认段数量；验证：把这几项数字写进阶段汇报，差异较大的换月和空档的处理方式单独列出请用户判断
- [x] 12.5 对 2026-09-04 之前的一个 RTH 交易日运行 `scan --scope pa_brooks:ES:RTH:5m`；验证：输出 `WAIT`，快照带实际合约、来源和内容摘要，重复运行摘要相同
- [x] 12.6 运行 `uv run pytest` 和 `uv run pytest -m realdata`；验证：全部通过，把实际摘要写进汇报。`uv run pytest -m webull_live` 预期失败（`MARKET_DATA_NOT_SUBSCRIBED`），留到确定实时来源后再处理（见 design.md 决策 18）
- [x] 12.7 按 `CLAUDE.md` 的"阶段完成判定"模板写阶段 1 汇报（完成项与未完成项、产物路径、命令与真实结果、新增假设及位置、下一阶段可复现的输入），在分支上提交，不合并到 `main`；验证：`git log main..phase-1-data-contract` 有提交，`main` 未变动
