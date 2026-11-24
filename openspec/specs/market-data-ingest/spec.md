# market-data-ingest Specification

## Purpose

把外部行情文件只读地导入为统一格式的 K 线，保留来源、版本和可用时点，使之后的每个决策都能追溯到它当时看到的数据。

## Requirements

### Requirement: K 线数据合同
每根入库的 K 线 SHALL 至少包含 `instrument_root`、`contract_id`、`session_type`、`timeframe`、`bar_start_utc`、`bar_end_utc`、`available_at_utc`、`available_at_estimated`、`open`、`high`、`low`、`close`、`volume`、`source`、`revision`。时间 MUST 以 UTC 存储，价格 MUST 以整数 tick 存储。

#### Scenario: 入库记录字段完整
- **WHEN** 一根 K 线成功导入
- **THEN** 查询该记录时上述字段全部非空

#### Scenario: 价格不在最小变动价位上
- **WHEN** 来源价格不能被该品种的最小变动价位整除
- **THEN** 该记录不入库，并记为 `OFF_TICK_PRICE` 异常

### Requirement: Databento DBN 来源
系统 SHALL 能读取 Databento `ohlcv-1m` 的 DBN 文件（含压缩形式），用文件自带的符号映射确定每条记录的合约。其他 schema 的 DBN 文件 MUST 被报告为 `UNSUPPORTED_SCHEMA` 且不导入。

#### Scenario: 导入历史 ES 文件
- **WHEN** 数据根目录下有 `GLBX-20260905-AKYFXCHGY4` 这批文件，运行导入
- **THEN** 其中单腿 ES 合约的 1 分钟线入库，`source` 标明 Databento 数据集与文件

#### Scenario: 不支持的 schema
- **WHEN** 数据根目录中有一个 `trades` schema 的 DBN 文件
- **THEN** 导入摘要将它列为 `UNSUPPORTED_SCHEMA`，其余文件照常导入

### Requirement: CSV 来源
系统 SHALL 能读取字段符合数据合同的 CSV 文件。缺少必需列的文件 MUST 整体拒收并指出缺少的列。

#### Scenario: 合法 CSV
- **WHEN** 导入一个含全部必需列的 CSV
- **THEN** 其中的 K 线入库，`source` 标明该文件

#### Scenario: 缺列
- **WHEN** CSV 缺少 `contract_id` 列
- **THEN** 该文件不导入任何记录，摘要指出缺少 `contract_id`

### Requirement: 只导入单腿合约
系统 SHALL 丢弃价差合约的记录，并在导入摘要中报告丢弃的代码数和记录数。

#### Scenario: 文件含日历价差
- **WHEN** 导入的文件含 `ESH0-ESM0` 的记录
- **THEN** 这些记录不入库，摘要中的价差丢弃计数大于零

### Requirement: 实际合约唯一标识
系统 SHALL 把来源代码解析为含四位年份的 `contract_id`（如 `ESH2020`），使相隔十年的同名代码对应不同合约。无法由符号映射确定合约的记录 MUST 被拒收并记为 `UNMAPPED_INSTRUMENT`。

#### Scenario: 同名代码跨十年
- **WHEN** 文件中 `ESH0` 在 2019–2020 年和 2024 年之后各有一段映射
- **THEN** 前一段的记录归入 `ESH2020`，后一段归入 `ESH2030`

#### Scenario: 映射区间外的记录
- **WHEN** 某条记录的时间不在其 instrument 的任何映射区间内
- **THEN** 该记录不入库，并记为 `UNMAPPED_INSTRUMENT`

### Requirement: 可用时点
来源提供了取数时刻时，系统 SHALL 以该 K 线最早的取数时刻作为 `available_at_utc`，并置 `available_at_estimated=false`。来源未提供时，SHALL 以"收盘时刻加配置的固定延迟"估计，并置 `available_at_estimated=true`。文件的导入时刻 MUST NOT 被当作 `available_at_utc`。

#### Scenario: 采集的 CSV 带取数时刻
- **WHEN** 导入的 CSV 行带有取数时刻 09:35:10
- **THEN** 该 K 线的 `available_at_utc` 为 09:35:10，`available_at_estimated` 为假

#### Scenario: 历史文件没有到达时刻
- **WHEN** 导入 Databento 历史文件，延迟配置为 5 秒
- **THEN** 每根 K 线的 `available_at_utc` 等于 `bar_end_utc` 加 5 秒，`available_at_estimated` 为真

### Requirement: 修订保留
同一来源、合约、周期、起始时刻的 K 线内容发生变化时，系统 SHALL 以递增的 `revision` 新增一条记录并保留旧记录，各自记录首次可见时刻。内容完全相同的重复记录 MUST 被跳过。

#### Scenario: 新文件修订了旧 K 线
- **WHEN** 新导入的文件中某根 K 线与库中同键记录的收盘价不同
- **THEN** 库中新增 `revision` 加一的记录，原记录保持不变

#### Scenario: 日期重叠且内容相同
- **WHEN** 每日文件与历史文件覆盖同一天且内容一致
- **THEN** 不新增任何记录，摘要报告跳过的重复数

### Requirement: 数据根目录增量导入
`ingest` SHALL 扫描配置的数据根目录及其子目录，只导入尚未处理的内容：未变化的文件被跳过，被追加过的文件只处理新增的行。

#### Scenario: 新增一个文件
- **WHEN** 数据根目录在上次导入后新增了一个文件，再次运行 `ingest`
- **THEN** 只有新文件被读取和导入，旧文件被报告为已跳过

#### Scenario: 当日 CSV 被追加
- **WHEN** 一个已导入过的 CSV 在末尾新增了 3 行，再次运行 `ingest`
- **THEN** 库中只新增这 3 行对应的 K 线

#### Scenario: 原样重跑
- **WHEN** 数据根目录没有变化时再次运行 `ingest`
- **THEN** 库中 K 线记录数和内容不变

### Requirement: 数据根目录在项目之外
真实行情和运行库 SHALL 位于项目目录之外的数据根目录。配置的数据根目录位于项目目录内时，系统 MUST 拒绝运行并说明原因。

#### Scenario: 数据根目录配置在项目内
- **WHEN** 数据根目录被配置为项目内的 `data/`
- **THEN** `ingest` 拒绝执行，提示数据根目录必须在项目之外

### Requirement: 每个交易日只用一个来源
同一 scope 的同一交易日有多个来源的数据时，系统 SHALL 按配置的来源优先级选定一个来源供该交易日整日使用，MUST NOT 在同一交易日内混用不同来源的 K 线。所选来源 MUST 记录在快照中。

#### Scenario: 两个来源覆盖同一天
- **WHEN** 某交易日 Databento 和 Webull 都有数据，优先级为 Databento 在前
- **THEN** 该交易日的 5 分钟线全部来自 Databento，快照记录来源为 Databento

#### Scenario: 只有一个来源
- **WHEN** 某交易日只有 Webull 的数据
- **THEN** 该交易日使用 Webull 的 K 线

### Requirement: 可选的开发可见截止日
配置了开发可见截止日时，晚于该日的 K 线 MUST NOT 进入运行库。未配置时，系统 SHALL 导入全部数据，并在导入摘要和可得性报告中标注 `HOLDOUT_BOUNDARY_NOT_SET`。

#### Scenario: 配置了截止日
- **WHEN** 以截止日 D 导入
- **THEN** 运行库中没有交易日晚于 D 的 K 线

#### Scenario: 未配置截止日
- **WHEN** 没有配置截止日时运行 `ingest`
- **THEN** 全部数据入库，导入摘要含 `HOLDOUT_BOUNDARY_NOT_SET`

### Requirement: 只读
`ingest` MUST NOT 修改、移动或删除数据根目录中的任何来源文件，且 MUST NOT 发起任何网络请求。

#### Scenario: 导入前后来源文件不变
- **WHEN** 完成一次导入
- **THEN** 数据根目录中每个来源文件的内容摘要与导入前相同

### Requirement: 合成样本标记
合成行情 SHALL 带 `FIXTURE` 标记，该标记 MUST 随数据进入快照和报告，使合成结果不会被当作真实证据。

#### Scenario: 导入合成样本
- **WHEN** 导入测试用的合成 CSV
- **THEN** 入库记录和由它生成的快照都带 `FIXTURE` 标记

### Requirement: 库外数据的内容摘要
导入时因晚于开发可见截止日而留在库外的 K 线，系统 SHALL 按来源、合约、周期和交易日累计一个与顺序无关的内容摘要，和覆盖统计一起保存。

#### Scenario: 顺序无关
- **WHEN** 同一批 K 线以不同顺序导入
- **THEN** 逐日内容摘要相同

### Requirement: 事后执行截止日
设置或提前开发可见截止日之后，`ingest` SHALL 把运行库中晚于截止日的原始 K 线转为库外覆盖记录（含内容摘要），并删除这些 K 线及其派生线、交易日状态、参考价位和日内异常。原始文件不动。

#### Scenario: 先全量导入后设截止日
- **WHEN** 运行库已有截止日之后的 K 线，带截止日运行 `ingest`
- **THEN** 这些 K 线从 `bars` 中消失，`withheld_coverage` 有对应的覆盖和摘要，摘要里报告移出的条数

#### Scenario: 已处理过的结果不留在库里
- **WHEN** 截止日之后存在交易日状态或参考价位
- **THEN** 它们被一并删除

### Requirement: 截止日之后的实时行情放行
`config/live_data.yaml` 登记的来源、从登记首日起的交易日，即使晚于开发可见截止日，导入时 SHALL 进运行库，只供常驻纸面运行、影子和发布后监控使用。同一天其他来源的数据 SHALL 照旧封存。截止日清理 SHALL 不移出放行的数据。切分登记 SHALL 把放行日排除在所有段之外。放行首日不晚于已登记封存的最后一天时，该品种 SHALL 不放行，导入摘要写明冲突。

#### Scenario: 放行首日之后的实时来源
- **WHEN** 导入 Webull 在放行首日及以后的 K 线，以及 Databento 同一天的 K 线
- **THEN** Webull 的进运行库，Databento 的只记封存覆盖

#### Scenario: 与已封存日期冲突
- **WHEN** 放行首日不晚于已登记封存的最后一天
- **THEN** 该品种不放行，数据照旧封存，导入摘要列出冲突
