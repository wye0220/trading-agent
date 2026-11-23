## ADDED Requirements

### Requirement: review 子命令
`review` SHALL 支持：`--day YYYY-MM-DD`（单日）、`--from`／`--to`（区间）、`--run-id`（缺省取该 scope 已冻结基线的主运行）、`--account`（带上人工轨）、`--seal`（复盘前先封存）。输出每个 session 的结论摘要和报告路径。

#### Scenario: 没有可用的运行
- **WHEN** 没有给 `--run-id`，该 scope 也没有已冻结的基线
- **THEN** 命令以配置错误退出
