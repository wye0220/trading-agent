# evaluation-protocol Specification

## Purpose

规定独立评测的预登记、协议标识和两阶段流程（1 个胶囊盲筛、6 个胶囊正式确认），评测在保留集下自己的数据库里回放，作者只收到摘要。

## Requirements

### Requirement: 解封前预登记
评测 SHALL 在任何窗口解封之前登记：候选摘要、基线、协议正文（阈值、成本、主指标、最低成交数、bootstrap 参数、α、可见字段、有效日规则）以及盲筛窗和确认窗的日期与内容摘要。登记 SHALL 不可修改。

#### Scenario: 候选在登记后被改动
- **WHEN** 候选的内容摘要与登记时不同
- **THEN** 评测拒绝，原因 `CANDIDATE_CHANGED`，不解封任何窗口

#### Scenario: 没有登记就运行
- **WHEN** 对没有登记的候选运行盲筛或确认
- **THEN** 拒绝，原因 `NOT_REGISTERED`

### Requirement: 两阶段流程与输出状态
盲筛 SHALL 只输出 `SCREEN_CONTINUE`、`SCREEN_REJECTED` 或 `EVIDENCE_INSUFFICIENT`，不含置信界或 p 值。正式确认 SHALL 只对盲筛为 `SCREEN_CONTINUE` 的登记运行，输出 `PASS_FOR_SHADOW`、`FAIL` 或 `EVIDENCE_INSUFFICIENT`。每个阶段每个登记 SHALL 只运行一次。

#### Scenario: 20 日好成绩不能晋级
- **WHEN** 盲筛窗内候选大幅优于基线
- **THEN** 结果是 `SCREEN_CONTINUE`，没有 `PASS_FOR_SHADOW`，结果里没有置信界

#### Scenario: 盲筛显著劣化
- **WHEN** 盲筛窗内配对净 R／日均差低于预登记的淘汰阈值，或回撤恶化超过阈值
- **THEN** 结果是 `SCREEN_REJECTED`，确认窗不解封

#### Scenario: 重复查看
- **WHEN** 同一登记的同一阶段再次运行
- **THEN** 拒绝，原因 `ALREADY_VIEWED`

#### Scenario: 跳过盲筛
- **WHEN** 盲筛还没有 `SCREEN_CONTINUE` 就运行确认
- **THEN** 拒绝，原因 `SCREEN_REQUIRED`

#### Scenario: 功效资格不足
- **WHEN** 登记时该轨道的功效资格不是 `ELIGIBLE`
- **THEN** 正式确认输出 `EVIDENCE_INSUFFICIENT`，确认窗不解封，不扣 α

### Requirement: α 记账
正式确认 SHALL 在解封确认窗之前登记一次正式检验，额度用完时拒绝；看到结果后记为已消耗。盲筛 SHALL NOT 扣 α。

#### Scenario: 额度用完
- **WHEN** 该 scope 该假设家族的正式检验次数已满
- **THEN** 确认被拒，原因 `ALPHA_EXHAUSTED`，确认窗不解封

### Requirement: 作者只见允许的摘要
运行库和项目内的报告 SHALL 只含协议列出的可见字段。逐日、逐笔和完整门槛数值 SHALL 只写在评测方目录。

#### Scenario: 盲筛摘要
- **WHEN** 盲筛完成
- **THEN** 运行库里的结果只有状态、原因码和有效日数，没有盈亏数字
