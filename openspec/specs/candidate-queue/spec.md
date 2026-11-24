# candidate-queue Specification

## Purpose

维护策略类候选的双轨队列：效应估计、按预登记公式排序、过期与基准替换后的 SUPERSEDED，以及只查可运行性的前向预演。

## Requirements

### Requirement: 验证轨道
`strategy_rule` 类的 `PROPOSED` 候选 SHALL 标注 `PRUNING_DEFENSIVE` 或 `ALPHA_EXPANSION` 并进入该 scope 的队列。其他类别 MUST NOT 进入策略队列。由合成数据得到的候选 MUST NOT 进入队列。

#### Scenario: data_engine 候选
- **WHEN** 候选类别是 `data_engine`
- **THEN** 队列里没有它

### Requirement: 效应估计
系统 SHALL 在开发段加向前验证段回放候选配置，与基线主运行逐日配对，给出平均日差、各段日差、成交数和回撤变化，并在台账记一条开发尝试。估计 MUST NOT 使用截止日之后的数据。

#### Scenario: 估计一次
- **WHEN** 对一个队列里的候选运行估计
- **THEN** 队列条目带上效应估计，台账多一条 `DEV_ATTEMPT`

### Requirement: 预登记的排序
排序分 SHALL 等于效应项 × 证据天数权重 × 机制可信度，公式和权重取自 `config/evolve.yaml`。没有效应估计的候选分数为空并排在最后。

#### Scenario: 有反例的候选排得更低
- **WHEN** 两个候选效应和天数相同，一个反例占比 50%，另一个为 0
- **THEN** 前者的分数是后者的一半

### Requirement: 过期与替换
在队列中超过 `queue_ttl_days`、或 `base_release` 已不是当前基准的候选 SHALL 转为 `SUPERSEDED`。

#### Scenario: 基线换了
- **WHEN** scope 的已冻结基线换成了新的
- **THEN** 以旧基线为基准的排队候选转为 `SUPERSEDED`

### Requirement: 队列前向预演
系统 SHALL 只在晚于基线末日、且不属于任何胶囊的已入库有效日上，对每个 scope 排名前三的候选做可运行性检查，并登记已观察的日期。没有符合条件的日期时返回 `NO_ELIGIBLE_DAYS`。预演 MUST NOT 报告盈亏。

#### Scenario: 没有可用日期
- **WHEN** 运行库里没有晚于基线末日的非胶囊有效日
- **THEN** 结果为 `NO_ELIGIBLE_DAYS`，不运行策略
