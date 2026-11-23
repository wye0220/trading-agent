# Proposal

## Why

阶段 1–3 有了数据、策略和引擎，但还没有一个你能用的入口：`scan` 只做数据校验，没有地方登记你的交易计划，也没有东西在盘中盯着你的真实持仓。提示词包的 Prompt 4 补上这一层。它也是"一期"里最先对抄底、拖止损、无新形态重试起作用的部分。

## What Changes

- **`scan` 接入策略和引擎**：数据校验通过后，用当前策略和纸面账本推进一步，输出 JSON 和中文短报（事实、决策、持仓、纪律、数据）。`WAIT` 指明缺什么条件和下一确认点，`BLOCKED` 说明风控原因。**BREAKING**：`scan` 不再只返回数据校验结果。
- **`plan`：人工交易前登记与预检**。支持一行快捷语法 `plan ES L 5000 4995 5010 1 PA Spike H1`。给出策略一致性、纪律标记、账户风险三方面的结论（`ALIGNED`、`DEVIATES`、`BLOCKED_BY_RISK`）。默认只预检；显式激活才预留预算。计划状态变化全部留审计。
- **人工成交导入**：只读导入成交和订单生命周期 CSV，按券商 ID 幂等；缺 ID 且无法唯一去重的隔离待核对；与事前计划关联，事后补录的计划不算。
- **`watch`：盘中只读纪律提醒**。按可调的间隔（60 秒、300 秒或自定义）读取账户快照，评估六条规则：`NAKED_POSITION`、`STOP_MOVED_AWAY`、`REENTRY_IN_COOLDOWN`、`UNPLANNED_ENTRY`、`COUNTERTREND_ADD`、`DAILY_LOSS_NEAR_LIMIT`。数据过期或读取失败时告警并标明数据时点，不做推断。同一持仓同一规则在静默窗口内不重复。
- **纪律事件与人工轨 KPI**：`plan_adherence` 和 `strategy_alignment` 分开记录；证据不足时标 `UNKNOWN`；每日写入 `plan_coverage` 等指标。
- **交易员纠正**：`trader_notes` 关联事件，只作复盘证据，不改任何规则或状态。
- **券商只读适配器接口**，加一个读取本地快照文件的适配器。接口只有读取方法；测试断言没有下单、改单、撤单的调用。
- 运行库 migration `0003`，配置 `config/manual.yaml`、`config/news_windows.yaml`，文档 `docs/scan-plan-watch.md`。

不在本次范围：

- **真实券商的适配器**。你的期货账户在哪家券商还没有定（Webull 没有期货行情），所以本阶段只做接口和"本地快照文件"适配器。
- 控制通道推送（Discord 等）和常驻调度是阶段 10。本阶段的提醒写进数据库并打印。
- 调用 Claude 生成解释是阶段 10。本阶段的中文短报由确定性代码生成。
- 不在真实历史数据上运行策略：带 `--as-of` 的历史扫描在没有设开发可见截止日时只做数据校验。

## Capabilities

### New Capabilities

- `paper-scan`: 把数据校验、策略和纸面账本合成一次扫描及其输出。
- `manual-plan-precheck`: 人工计划的登记、预检、激活、过期与审计。
- `manual-trade-import`: 人工成交与订单事件的只读导入、去重、计划关联。
- `broker-watch`: 只读账户快照的采集与盘中纪律提醒规则。
- `discipline-tracking`: 纪律事件、交易员纠正、人工轨 KPI。

### Modified Capabilities

- `agent-cli`: `scan` 的行为变化；`plan`、`watch` 从未实现变为可用；`ingest` 增加导入人工成交的选项。
- `runtime-storage`: 新增人工轨所需的表和列。

## Impact

- **代码**：新增 `src/trading_agent/manual/`（计划、导入、纪律、KPI、watch、券商适配器）和 `src/trading_agent/scan.py` 的扩展；`cli.py` 接上三个子命令。
- **存储**：migration `0003`。这是阶段 3 之后的第一次结构变更，按编号 migration 进行，并重跑全部既有测试。
- **依赖**：无新增。
- **需要你回答**（见 design.md 的 Open Questions）：真实账户在哪家券商、默认账户标识、风控限额与费用、开发可见截止日。
