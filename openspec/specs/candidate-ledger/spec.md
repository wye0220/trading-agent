# candidate-ledger Specification

## Purpose

保存只改一个白名单键的候选及其只追加的事件台账，校验提案，并按 scope 和假设家族统计开发尝试、正式检验次数和余下的 α 额度。

## Requirements

### Requirement: 候选校验
每个候选 SHALL 通过 `Candidate` schema，并且只改一个键。策略类候选的差异 MUST 落在该策略登记的可改键白名单内、`from` MUST 等于现行配置的值、用差异 MUST 能构造出合法的策略变体。候选引用的证据 MUST 属于本次的某个聚类。不合格的候选 MUST 被拒绝并说明原因。

#### Scenario: 键不在白名单
- **WHEN** 提案想改 `params.discipline.max_stops_per_day`
- **THEN** 提案被拒绝，原因 `NOT_WHITELISTED`

#### Scenario: from 与现行配置不符
- **WHEN** 提案的 `from` 是 20，现行配置是 30
- **THEN** 提案被拒绝，原因 `FROM_MISMATCH`

#### Scenario: 证据不够
- **WHEN** 提案引用的聚类只有 2 天证据
- **THEN** 候选以 `OBSERVATION` 状态保存，不进队列

### Requirement: 数量上限与 NO_CANDIDATE
每个策略家族每次运行 SHALL 最多新生成 3 个候选，按聚类证据天数从多到少取。没有任何候选时结果 MUST 是 `NO_CANDIDATE`。

#### Scenario: 四个合格模板
- **WHEN** 一次运行有 4 个合格的模板提案
- **THEN** 只生成证据天数最多的 3 个，第 4 个记为 `LIMIT_REACHED`

#### Scenario: 没有可升级的聚类
- **WHEN** 没有任何聚类达到 `min_days`
- **THEN** 结果为 `NO_CANDIDATE`，不写候选

### Requirement: 候选落盘
通过校验的候选 SHALL 写入 `candidates` 表，并在 `candidates/<scope>/` 下生成 JSON 和 Markdown。候选标识由 scope、类别和差异决定；重复运行 MUST NOT 产生重复候选。

#### Scenario: 再跑一次
- **WHEN** 同样的输入再运行一次
- **THEN** 候选数不变，台账不新增 `CREATED`

### Requirement: 台账只追加
候选的创建、状态变更、证据刷新、开发尝试、正式检验的登记与消耗 SHALL 各在 `candidate_ledger` 追加一条。失败和放弃的候选 MUST 保留。

#### Scenario: 状态变更
- **WHEN** 一个候选从 `PROPOSED` 变为 `SUPERSEDED`
- **THEN** 台账多一条 `STATUS_CHANGED`，带前后状态和原因

### Requirement: α 额度统计
系统 SHALL 按 scope 和假设家族统计开发尝试数、已登记和已消耗的正式检验数，以及余下的 α。登记正式检验 MUST 在额度用完时被拒绝。首次查看结果后该检验记为已消耗。

#### Scenario: 额度用完
- **WHEN** 一个家族已登记 5 次正式检验，上限为 5
- **THEN** 第 6 次登记被拒绝，错误码 `ALPHA_EXHAUSTED`

### Requirement: 失败后重提
同一 scope、同一机制键的候选曾经 FAIL 时，新的候选 MUST 引用之前的失败并说明差异，否则被拒绝。

#### Scenario: 没有引用失败报告
- **WHEN** 同机制的候选状态为 `FAIL`，新提案没有 `prior_failure`
- **THEN** 新提案被拒绝，原因 `PRIOR_FAILURE_NOT_REFERENCED`
