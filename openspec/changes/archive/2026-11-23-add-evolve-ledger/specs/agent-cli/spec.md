## ADDED Requirements

### Requirement: evolve 子命令
CLI SHALL 提供 `evolve`：默认做聚合并生成候选；`--last N` 指定窗口；`--import-proposals FILE` 导入提案；`--estimate` 为排队候选估计效应；`--pre-eval-shadow` 运行前向预演；`--ledger` 只显示台账统计。输出 JSON 或中文短报。

#### Scenario: 默认运行
- **WHEN** 运行 `evolve`
- **THEN** 输出聚类数、可升级数、新候选和队列，并写出周报

#### Scenario: 没有已冻结的基线
- **WHEN** scope 没有已冻结的基线
- **THEN** 命令拒绝，错误码 `NO_BASELINE`
