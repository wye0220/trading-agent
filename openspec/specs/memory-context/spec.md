# memory-context Specification

## Purpose

按 scope 和 memory_version 加载有效的 Memory 条目，用模板生成句子，只向任务注入相关条目并核对输出里的引用。

## Requirements

### Requirement: 按 scope 和版本加载有效条目
加载 SHALL 取指定（缺省为头指针的）`memory_version` 的成员，只留 scope 为 `global` 或当前四维 scope、且 `valid_until` 未过的条目，并返回 `memory_version`、`scope_digest` 和被排除的计数。加载时 SHALL 再校验一次，不合格的不加载。

#### Scenario: 过期条目不加载
- **WHEN** 条目的 `valid_until` 早于加载时刻
- **THEN** 该条目不在结果里，`excluded.EXPIRED` 加一，`memory_version` 不变

#### Scenario: 其他 scope 的条目
- **WHEN** 条目的 scope 是另一个四维 scope
- **THEN** 该条目不在结果里

#### Scenario: 还没有任何 Memory
- **WHEN** 从未写入过快照
- **THEN** 返回空列表，`memory_version` 为空

### Requirement: 句子由模板从字段生成
展示给模型的句子 SHALL 只由条目字段和登记表里的标签经模板生成，并带 `[item_id]`。

#### Scenario: 句子与字段一致
- **WHEN** 为一个条目生成句子
- **THEN** 句子含该条目的指标标签、值、单位，以及非空的条件、样本数、区间和来源；改动任一字段后句子随之变化

### Requirement: 只注入与任务相关的条目并核对引用
Context 构建器 SHALL 按登记的任务相关性筛选条目，数量不超过登记的上限，并给出可引用的 `item_id` 列表。输出引用的 `item_id` SHALL 属于本次注入。

#### Scenario: 无关条目不注入
- **WHEN** 为 `ScanExplanation` 构建 Context，而库里有 `watch_poll_interval_seconds` 偏好
- **THEN** 该偏好不在注入的条目里

#### Scenario: 引用了没有注入的条目
- **WHEN** 输出引用的 `item_id` 不在本次注入的列表里
- **THEN** 核对失败，原因 `UNKNOWN_MEMORY_CITATION`
