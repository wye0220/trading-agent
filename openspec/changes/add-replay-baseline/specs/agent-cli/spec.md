## ADDED Requirements

### Requirement: replay 子命令
`replay --from YYYY-MM-DD --to YYYY-MM-DD` SHALL 在该区间回放并输出运行摘要。真实数据需要 `--allow-real-data` 且区间不晚于开发可见截止日。

#### Scenario: 缺少区间
- **WHEN** 没有给 `--from` 或 `--to`
- **THEN** 命令以配置错误退出，不写数据

### Requirement: baseline 子命令
`baseline` SHALL 依次登记切分、冻结基线、校验两次运行一致、跑扰动并生成基线报告和功效报告。`--skip-power` 跳过扰动和功效测算，`--jobs N` 并行跑扰动。

#### Scenario: 输出
- **WHEN** 命令成功
- **THEN** 输出 `baseline_id`、状态和两份报告的路径
