## ADDED Requirements

### Requirement: 发布清单
`release --prepare` SHALL 生成发布清单：`release_id`、`previous_release`、`git_commit`、版本元组与各项摘要、四维 scope、配置差异、评测与影子报告摘要、回滚目标、`rollback_trigger`、预期区间、改动说明和可观察的代价。`manifest_hash` SHALL 是清单规范化 JSON 的 sha256，同样的输入得到同样的摘要。

#### Scenario: 候选清单
- **WHEN** 影子结果为 `PASSED` 的候选运行 `release --prepare`
- **THEN** 输出清单和 `manifest_hash`，`previous_release` 是当前 active 版本

### Requirement: 首次纸面基线
scope 没有 active 时 SHALL 允许 `release_type=INITIAL_PAPER_BASELINE`、`previous_release=null`，绑定 `baseline_id`，标注 `UNVALIDATED_BASELINE`，预期区间取自阶段 5 开发段的基线报告。清单 SHALL 列出工程、逐 bar 向量、账本、只读权限和纸面运行五项验收，任一不通过 SHALL 不能批准。此路径 SHALL 不能覆盖已存在的 active。

#### Scenario: 重复首发
- **WHEN** scope 已有 active 时批准首次基线
- **THEN** 拒绝，原因 `ACTIVE_EXISTS`

### Requirement: 人工批准
批准 SHALL 只来自独立 CLI（发起者是登记的批准人、在交互终端里输入清单摘要前缀）或经身份校验的控制通道，且 SHALL 指定 `candidate_id`（或 `baseline_id`）和 `manifest_hash`。批准 SHALL 在发布库的一个事务里核验：发布服务代码与受保护的摘要清单一致；按当前状态重算的清单摘要等于给定值；候选所基于的版本仍是当前 active；评测和影子结果未过期；然后更新 active 指针并追加审计事件。重复批准 SHALL 幂等。

#### Scenario: 无人工批准
- **WHEN** 不在交互终端里、或发起者不是批准人
- **THEN** 拒绝，原因 `NO_HUMAN_APPROVAL`，指针不变

#### Scenario: 候选哈希被改
- **WHEN** 候选在准备清单之后被改动
- **THEN** 拒绝，原因 `MANIFEST_MISMATCH`

#### Scenario: 基线被更新
- **WHEN** 候选所基于的版本不再是当前 active
- **THEN** 拒绝，原因 `STALE_BASE`

#### Scenario: 批准重跑
- **WHEN** 同一清单再批准一次
- **THEN** 返回 `ALREADY_ACTIVE`，不写新的事件

### Requirement: 盘中读取完整的已提交版本
盘中扫描 SHALL 在发布库的一个读事务里解析 active 指针和清单，并用同一快照构造策略和版本元组。新版本 SHALL 从下一次扫描起生效；已存在的订单和仓位 SHALL 保留生成时的版本元组并按原规则完成。各 scope 的指针 SHALL 互不影响。

#### Scenario: 扫描中切换版本
- **WHEN** 一次扫描进行中另一进程批准了新版本
- **THEN** 这次扫描整体使用旧版本，下一次扫描使用新版本
