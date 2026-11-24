## Why

阶段 8 的正式确认只给出 `PASS_FOR_SHADOW`，候选还不能改变盘中使用的策略。阶段 9 把评测通过的候选接到盘中：影子纸面运行验收、带摘要的发布清单、经身份校验的人工批准、按 scope 的 active 指针切换、冻结与回滚，以及发布后的滚动监控和自动冻结。

## What Changes

- 新增 `release/` 模块和 `shadow`、`release`、`rollback`、`freeze`、`monitor` 五个子命令。
- 正式影子：基线臂和候选臂读同一批后续行情、各写各的账本；窗口长度、最低成交数和检查项在登记时冻结，到预登记终点才判定，不能延长。
- 队列前向预演的实时版：队列前三名挂在新行情上只查可运行性。
- 发布清单 `release_manifest`（含 `INITIAL_PAPER_BASELINE` 首次纸面基线）和 `manifest_hash`；批准在发布库内一个事务里核验并切换 active 指针、写审计事件，重复批准幂等。
- 冻结按原因分开记录和解除；回滚回到上一已批准的纸面版本，没有上一版本时冻结并停用该 scope 的入场。
- 盘中扫描在发布库的一个读快照里解析当前 active 版本，下一步起使用新版本；已有订单和仓位保持原版本。
- `monitor` 每个交易日收盘后计算滚动指标，触及回滚触发条件时自动冻结并告警，回滚与否由你决定。
- 新增 `config/release.yaml`、runtime migration `0009_release`、release migration `0002_release`、`docs/release-policy.md`。

## Capabilities

### New Capabilities
- `release-shadow`: 正式影子与实时前向预演。
- `release-approval`: 发布清单、人工批准、active 指针切换与版本解析。
- `release-freeze-rollback`: 冻结原因与解除政策、回滚。
- `release-monitor`: 发布后滚动指标、预期区间与自动冻结。

### Modified Capabilities
- `agent-cli`: 五个子命令。
- `runtime-storage`: migration `0009_release` 与发布库 migration `0002_release`。

## Impact

- 不改 `engine/`、`strategies/`；基线不失效。版本切换和冻结检查在 `scanner.py` 与引擎已有的提交前冻结重读之间完成。
- 真实运行库没有截止日之后的行情，常驻纸面运行没有处理过任何交易日，所以真实的首次纸面基线和影子都还做不了；本阶段的晋级演练全部用独立的测试数据库和发布库。
- 批准和回滚要你在终端里亲手输入清单摘要的前缀；代理（包括我）通过命令行跑不过这一步。
