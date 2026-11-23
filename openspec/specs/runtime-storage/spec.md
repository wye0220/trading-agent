# runtime-storage Specification

## Purpose

为行情、扫描、纸面账本、人工交易、候选和发布状态提供本地持久化，并保证并发写入安全、重跑不产生重复结果、发布状态与业务数据隔离。

## Requirements

### Requirement: 运行库表
运行库 SHALL 包含表：`market_snapshots`、`reference_levels`、`scan_events`、`paper_orders`、`paper_fills`、`trader_plans`、`manual_trades`、`manual_order_events`、`broker_snapshots`、`discipline_alerts`、`trader_notes`、`sessions`、`candidates`、`candidate_ledger`、`memory_items`、`evaluation_runs`、`alerts`、`freeze_events`、`dead_letter_queue`，以及存放 K 线和导入文件记录的表。`scan_events` MUST 含 `strategy_family` 和 `strategy_state_json` 字段。

#### Scenario: 初始化后表齐全
- **WHEN** 在空目录初始化运行库
- **THEN** 上述每张表都存在

#### Scenario: 重复初始化
- **WHEN** 对已初始化的运行库再次执行初始化
- **THEN** 已有数据不变，不报错

### Requirement: 发布库独立
active 指针和 `release_events` SHALL 存放在与运行库分开的发布数据库中，active 指针按四维 scope 索引。阶段 1 的任何命令 MUST NOT 写入 active 指针。

#### Scenario: 两个库分开
- **WHEN** 初始化完成
- **THEN** 运行库中没有 active 指针表，发布库中没有业务表

#### Scenario: 导入和扫描不碰发布状态
- **WHEN** 运行 `ingest` 和 `scan`
- **THEN** 发布库的内容与运行前相同

### Requirement: 并发安全
两个数据库 SHALL 启用 WAL 和 `busy_timeout`。带状态的关键记录 MUST 含 `version` 字段，状态变更与对应事件 MUST 在同一事务内提交。

#### Scenario: 读写并发
- **WHEN** 一个进程正在导入，另一个进程同时查询 K 线
- **THEN** 查询成功返回已提交的数据，不因锁而失败

#### Scenario: 过期版本的写入
- **WHEN** 一次更新携带的 `version` 与库中当前值不一致
- **THEN** 更新被拒绝，记录保持不变

### Requirement: 幂等
以相同输入重复运行同一操作 SHALL 不产生重复的 K 线、快照、订单或结算记录。

#### Scenario: 重复导入同一文件
- **WHEN** 同一文件被导入两次
- **THEN** K 线记录数与第一次导入后相同

### Requirement: 风险域隔离
纸面、人工和影子／回放的记录 SHALL 以风险域和运行 ID 区分，使一个域的记录不会被另一个域的查询混入。

#### Scenario: 不同运行模式的记录并存
- **WHEN** 库中同时有 `fixture` 模式和 `paper` 模式的运行记录
- **THEN** 按运行模式或运行 ID 查询时只返回对应域的记录

### Requirement: 结构变更可追踪
数据库结构 SHALL 通过编号的 migration 建立和变更，库内记录已应用的 migration 编号。

#### Scenario: 查询结构版本
- **WHEN** 初始化完成后查询结构版本
- **THEN** 返回已应用的 migration 编号列表，首项为初始 migration

### Requirement: 数据与凭证不进版本控制
运行库和原始行情 SHALL 存放在项目之外的数据根目录。发布库、持仓快照和凭证 MUST 被排除在 Git 之外。

#### Scenario: 初始化并导入后检查工作区
- **WHEN** 完成初始化和一次导入后运行 `git status`
- **THEN** 未跟踪列表中没有数据库文件、行情文件或凭证文件

#### Scenario: 运行库位置
- **WHEN** 初始化完成
- **THEN** 运行库文件位于数据根目录下，项目目录内没有运行库文件

### Requirement: 引擎表
运行库 SHALL 另含引擎所需的表：引擎运行记录、纸面仓位、已完结的纸面交易、解释请求队列、账户风险日记录和风险预留。纸面订单 SHALL 记录激活时刻、过期时刻、信号 K 时刻、触发价、限价、止损、目标描述和标记。这些结构 MUST 由一个新的编号 migration 建立，已有数据不受影响。

#### Scenario: 从阶段 1 的库升级
- **WHEN** 对一个只应用过初始 migration、且已有行情数据的运行库打开连接
- **THEN** 新的 migration 被应用，新表存在，原有的 K 线数量不变

#### Scenario: 结构版本
- **WHEN** 升级完成后查询结构版本
- **THEN** 已应用的编号依次为初始 migration 和引擎 migration

### Requirement: 人工轨的表
运行库 SHALL 另含计划审计、纪律事件和 watch 状态的表，并给计划、人工成交、订单事件和纪律提醒补充所需的列。这些结构 MUST 由新的编号 migration 建立，已有数据不受影响。

#### Scenario: 从阶段 3 的库升级
- **WHEN** 对一个已应用前两个 migration 的运行库打开连接
- **THEN** 第三个 migration 被应用，新表存在，原有数据不变

### Requirement: 研究用表
运行库 SHALL 通过编号 migration `0004_research.sql` 新增 `baselines`、`research_runs`、`split_registries`、`data_segments` 四张表，并给 `withheld_coverage` 增加 `content_sha256` 列。既有表的其他结构不变。

#### Scenario: 从阶段 4 的库升级
- **WHEN** 用新代码打开阶段 4 建的运行库
- **THEN** migration 0004 被应用，既有数据保留，全部既有测试通过

### Requirement: 复盘用表
运行库 SHALL 通过编号 migration `0005_review.sql` 新增 `session_reviews`、`observations`、`incident_drafts`、`memory_candidates` 四张表。既有表结构不变。

#### Scenario: 升级
- **WHEN** 用新代码打开阶段 5 的运行库
- **THEN** migration 0005 被应用，既有数据保留
