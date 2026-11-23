## ADDED Requirements

### Requirement: 人工轨聚类生成 Memory 候选
`evolve` SHALL 为可升级、有暴露条件的人工轨聚类生成 `discipline_pattern` 的 Memory 候选（出现天数占比、样本数、窗口区间、证据 id），状态 `PENDING_CONFIRMATION`。没通过 Memory 校验的 SHALL 跳过并按原因计数。重复运行 SHALL NOT 产生重复候选。

#### Scenario: 样本不足
- **WHEN** 聚类的暴露天数小于 Memory 的样本门槛
- **THEN** 不生成候选，周报里计入 `EVIDENCE_INSUFFICIENT`

#### Scenario: 样本足够
- **WHEN** 聚类的暴露天数达到门槛
- **THEN** 生成一条 Memory 候选，再次运行不新增
