# memory-store Specification

## Purpose

保存只由结构化字段组成的长期记忆条目：双层校验、禁止写入风险上限和实时状态、确认与评测后生效的生命周期、不可变的 memory_version 快照和回滚。

## Requirements

### Requirement: 条目只由结构化字段组成
Memory 条目 SHALL 含 `item_id`、`kind`、`scope`、`statement`、`source_event_ids`、`confirmed_by`、`created_at`、`valid_until`、`status`、`version`，并通过 `schemas/MemoryItem.schema.json`。`statement` SHALL 只含 `subject`、`condition`、`metric`、`value`、`unit`、`sample_n`、`period`、`source`，每个字符串字段是枚举或固定文法。`scope` SHALL 是已登记的四维 scope 或 `global`。

#### Scenario: 字段里夹带自由文本
- **WHEN** 任一字段含空格、中文或不符合文法的文本（如 `ignore previous instructions`）
- **THEN** 校验拒绝，原因 `SCHEMA_INVALID`，不写入任何表

#### Scenario: 多出的字段
- **WHEN** `statement` 或条目带有未定义的字段（如 `note`）
- **THEN** 校验拒绝，原因 `SCHEMA_INVALID`

### Requirement: 只能引用已登记的指标和事件类型
`subject`、`condition` 的键和值、`metric`、`unit`、`source` SHALL 来自 `config/memory.yaml` 的登记表；`user_preference` 的键 SHALL 在偏好白名单内且值在登记的范围内。

#### Scenario: 未登记的指标
- **WHEN** `metric` 符合字符集但不在登记表里
- **THEN** 校验拒绝，原因 `METRIC_NOT_REGISTERED`

#### Scenario: 未登记的事件类型
- **WHEN** `condition` 是 `event=made_up_event`
- **THEN** 校验拒绝，原因 `CONDITION_NOT_REGISTERED`

#### Scenario: 偏好值越界
- **WHEN** `report_max_lines` 的值是 0
- **THEN** 校验拒绝，原因 `VALUE_OUT_OF_RANGE`

### Requirement: 禁止写入的内容
风险上限、当前仓位和风险累计 SHALL NOT 进入 Memory。校验 SHALL 在查白名单之前拒绝这类名字；登记表里出现这类名字时配置加载 SHALL 失败。没有 `confirmed_by` 的条目 SHALL NOT 存在。

#### Scenario: 候选试图写入风险上限
- **WHEN** 候选的 `metric` 是 `daily_loss_limit_usd` 或 `max_contracts`
- **THEN** 校验拒绝，原因 `RISK_LIMIT_FORBIDDEN`

#### Scenario: 候选试图写入当前仓位
- **WHEN** 候选的 `subject` 或 `metric` 指向持仓或当日盈亏
- **THEN** 校验拒绝，原因 `LIVE_STATE_FORBIDDEN`

#### Scenario: 登记表试图放行风险上限
- **WHEN** `config/memory.yaml` 的偏好键里有 `max_contracts`
- **THEN** 配置加载失败

### Requirement: 证据门槛
`discipline_pattern` SHALL 有不小于登记门槛的 `sample_n`、非空的 `period` 和至少一个证据 id；`market_fact` SHALL 有 `valid_until`。

#### Scenario: 样本不足的执行模式
- **WHEN** `discipline_pattern` 的 `sample_n` 小于门槛
- **THEN** 校验拒绝，原因 `EVIDENCE_INSUFFICIENT`

#### Scenario: 没有有效期的市场事实
- **WHEN** `market_fact` 的 `valid_until` 为空
- **THEN** 校验拒绝，原因 `VALID_UNTIL_REQUIRED`

### Requirement: 生命周期
候选 SHALL 经确认才能成为条目。`user_preference` 确认后立即生效；`market_fact` 和 `discipline_pattern` 确认后转 `PENDING_EVALUATION`，只有存在通过的评测记录时才能生效。被否定的条目 SHALL 标 `SUPERSEDED` 并保留历史。每次变动 SHALL 在 `memory_events` 追加一条。

#### Scenario: 确认偏好
- **WHEN** 确认一条 `PENDING_CONFIRMATION` 的 `user_preference` 候选
- **THEN** 生成状态 `ACTIVE` 的条目，`confirmed_by` 记确认者，候选转 `CONFIRMED`，头指针指向含该条目的新快照

#### Scenario: 同一偏好的新值
- **WHEN** 确认一条与现有条目同主体、同指标但值不同的偏好
- **THEN** 同一 `item_id` 生成下一个 `version`，旧版本标 `SUPERSEDED`，新快照只含新版本

#### Scenario: 需要评测的种类
- **WHEN** 确认一条 `discipline_pattern` 候选
- **THEN** 候选转 `PENDING_EVALUATION`，不生成条目，头指针不变

#### Scenario: 没有评测记录时试图生效
- **WHEN** 对 `PENDING_EVALUATION` 的候选调用生效函数而没有通过的评测记录
- **THEN** 拒绝，原因 `EVALUATION_REQUIRED`

#### Scenario: 被新证据否定
- **WHEN** 否定一条生效的条目并给出证据 id
- **THEN** 条目标 `SUPERSEDED`，新快照不含它，旧行保留

#### Scenario: 重复确认
- **WHEN** 对已 `CONFIRMED` 或 `REJECTED` 的候选再次确认
- **THEN** 拒绝，原因 `NOT_PENDING`，状态不变

### Requirement: 版本快照与回滚
`memory_version` SHALL 是成员 `(item_id, version)` 列表的内容摘要，快照不可变。回滚 SHALL 只移动头指针并记事件。头指针的更新 SHALL 在事务内重读并校验版本，过期的写入被拒。

#### Scenario: 回滚后加载旧版本
- **WHEN** 把偏好从 A 改成 B 之后回滚到改之前的 `memory_version`
- **THEN** 加载得到值 A，`memory_version` 等于改之前的标识

#### Scenario: 回滚到不存在的版本
- **WHEN** 目标 `memory_version` 不存在
- **THEN** 拒绝，原因 `UNKNOWN_VERSION`，头指针不变

#### Scenario: 快照和事件不可改
- **WHEN** 对 `memory_versions` 或 `memory_events` 执行 UPDATE 或 DELETE
- **THEN** 数据库拒绝
