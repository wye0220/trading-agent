# evolve-aggregation Specification

## Purpose

把已封存并已复盘的 session 里的观察跨日聚类，按升级规则决定哪些聚类够格支撑候选，并把归纳输入放进解释队列。不调用模型，同样的输入得到同样的聚类。

## Requirements

### Requirement: 输入范围
`evolve` SHALL 只读取某个 scope 最近 N 个状态为 `SEALED` 且已有复盘的 session。策略轨 session 的交易日晚于开发可见截止日、或运行模式为 `pre_eval_shadow`／`live_shadow` 时 MUST 被排除。它 MUST NOT 读取胶囊、保留集或留在库外的行情。

#### Scenario: 截止日之后的策略 session
- **WHEN** 窗口里有一个交易日晚于开发可见截止日的策略轨 session
- **THEN** 它不进入聚类，结果里计入 `excluded`

#### Scenario: 没有可用的 session
- **WHEN** scope 没有已封存并已复盘的 session
- **THEN** 命令拒绝，错误码 `NO_SESSIONS`

### Requirement: 确定性聚类
系统 SHALL 按 `scope × mechanism × reason × diff_key` 聚类观察，合并来源事件和日期，统计出现天数和次数。不同 scope 的观察 MUST NOT 进入同一个聚类。相同输入 MUST 得到相同的聚类标识和统计。

#### Scenario: 同一机制不同原因
- **WHEN** 两天的 `FILTERED_SETUP` 观察原因不同
- **THEN** 它们落在两个聚类里

#### Scenario: 重复运行
- **WHEN** 对同一组 session 运行两次
- **THEN** 两次的输入摘要、聚类标识和统计完全相同

### Requirement: 反例与样例
每个聚类 SHALL 保留反例：窗口内满足该机制暴露条件却没有出现该机制的 session。没有暴露条件的机制，反例记为未知。每个聚类送去归纳的内容 MUST 只含统计摘要和最多 3 条正例、3 条反例。

#### Scenario: 有到期订单但之后没有越过触发价
- **WHEN** 某天有到期订单，却没有 `EXPIRED_THEN_TRIGGERED` 观察
- **THEN** 这一天计入该聚类的反例

#### Scenario: 样例上限
- **WHEN** 一个聚类有 100 天证据
- **THEN** 它的归纳输入里正例不超过 3 条、反例不超过 3 条

### Requirement: 升级规则
出现在至少 `min_days` 个不同交易日的聚类 SHALL 标为 `PROMOTABLE`，否则为 `OBSERVATION`。只有 `PROMOTABLE` 的聚类才能支撑 `PROPOSED` 候选。

#### Scenario: 只出现两天
- **WHEN** 一个聚类只在 2 个交易日出现，`min_days` 为 3
- **THEN** 它是 `OBSERVATION`，不生成 `PROPOSED` 候选

### Requirement: 跨品种候选组
同一策略家族、同一机制、同一差异键的 `PROMOTABLE` 聚类出现在多个品种时，系统 SHALL 把它们标为一个候选组，组内每个 scope 各自生成候选并分别评测。差异键不是品种专属时，组可以标注 `CROSS_ASSET_POOLED` 申请。

#### Scenario: ES 与 NQ 同一机制
- **WHEN** `pa_brooks:ES` 和 `pa_brooks:NQ` 各有同机制同差异键的可升级聚类
- **THEN** 两个聚类带同一个 `group_id`，各自 scope 生成自己的候选

### Requirement: 归纳输入入队
对每个 `PROMOTABLE` 聚类，系统 SHALL 把归纳输入放进解释队列（类型 `Candidate`），超过 token 预算的不入队。系统 MUST NOT 自行调用模型。

#### Scenario: 入队
- **WHEN** 一个聚类可升级且输入在预算内
- **THEN** 解释队列里有一条对应的 `PENDING` 请求，重复运行不重复入队
