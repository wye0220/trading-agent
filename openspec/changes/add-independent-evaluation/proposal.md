## Why

候选队列（6B）和 Memory 的"等待评测"（阶段 7）都停在评测入口前。阶段 8 提供独立评测：预登记协议、20 日盲筛加独立确认窗、Track A／B 门槛、事故回归库，以及评测身份与作者身份的隔离校验。

## What Changes

- 新增 `evaluation/` 模块和 `evaluate` 子命令：登记、盲筛、正式确认、标注类评测、回归、隔离检查、摘要清单。
- 新增 `incidents/` 回归库：把你确认的事故草稿收录为可独立重放的条目；重放不出来的标 `INCOMPLETE`。
- 新增 `config/evaluation.yaml`（协议预设）和 migration `0008_evaluation`（协议、登记、窗口账本、事故库）。
- 新增 `docs/evaluation-protocol.md`。
- 评测在评测方自己的数据库里回放（位于 `holdout_root` 下），作者只拿到预登记允许的摘要。

## Capabilities

### New Capabilities
- `evaluation-protocol`: 预登记、两阶段流程、α 记账、输出状态。
- `evaluation-windows`: 窗口分配、解封账本、退回、已披露窗口不得复用。
- `evaluation-gates`: 配对日差、区块 bootstrap、Track A／B 与通用门槛、差异交易检验、标注类指标。
- `evaluation-isolation`: 评测身份、受保护的摘要清单、隔离验收。
- `incident-regression`: 事故回归库与合成回归。

### Modified Capabilities
- `agent-cli`: `evaluate` 子命令。
- `runtime-storage`: migration `0008_evaluation`。

## Impact

- 不改 `engine/`、`strategies/`；基线不失效。
- 真实确认窗的评测需要三个前提：隔离验收通过、摘要清单由你写入、候选所在轨道的功效资格为 `ELIGIBLE`。现在三个都不满足，所以阶段 8 的真实结果只有回归库和合成自测，不解封任何真实胶囊。
