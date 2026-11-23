## Why

用户的真实操作在 Alpaca 模拟盘上，交易 SPY 和 QQQ（2026-11-23 确认）。人工轨（`plan`、`watch`、成交导入、纪律指标）目前只认期货：品种必须有合约月和换月规则，价格按期货的 tick 价值算风险。不支持股票和 ETF，人工轨就没有真实数据，阶段 6A 的人工轨复盘也只能用合成数据验收。

## What Changes

- 品种登记支持股票类（`kind: equity`）：没有合约月和换月，合约标识就是代码；tick 0.01 美元，每股每点 1 美元。登记 SPY、QQQ。
- `plan` 支持股票：数量是股数，每股计划风险 = 止损距离 + 滑点预算，费用按 Alpaca 的零佣金登记。
- 账户读取：新增 Alpaca 只读适配器，读持仓、挂单和订单历史，生成 `watch` 用的账户快照和成交导入用的记录。只用读取接口；代码里不出现下单、改单、撤单调用，测试断言这一点。
- 人工轨与策略轨的关系：SPY、QQQ 的人工交易只评价"是否按自己的计划做"（`plan_adherence`）。"是否符合正式策略"（`strategy_alignment`）一律为 `UNKNOWN`，因为正式策略只在 ES 上定义和建基线；不拿 ES 的信号去套 SPY。
- 账户限额按账户分开配置：期货的限额和股票账户的限额互不影响。

不做：不为 SPY、QQQ 建策略 scope、回放和基线；不采集它们的行情用于策略；不接任何下单功能。

## Capabilities

### New Capabilities
- `equity-instruments`: 股票类品种的登记、合约标识和风险计算。
- `alpaca-readonly`: Alpaca 账户的只读读取与快照、成交转换。

### Modified Capabilities
- `manual-plan-precheck`: 计划支持股票类品种；策略一致性在没有对应 scope 时为 `UNKNOWN`。
- `risk-controls`: 账户限额按账户配置。

## Impact

- 代码：`config.py`、`contracts.py`（品种类型）、`manual/`（计划、成交、快照）、新增 Alpaca 适配器、`cli.py`。
- 基线：`config.py` 和 `contracts.py` 属于决定回放结果的代码，改动后基线标识会变，需要重新冻结一次（约 35 分钟，ES 的结果预期不变，由两次回放比对确认）。
- 配置：`config/instruments.yaml` 增加 SPY、QQQ；`config/risk.yaml` 的账户限额改为按账户；`config/manual.yaml` 的默认账户保持 `alpaca-paper`。
- 与项目约定的关系：`CLAUDE.md` 规定首版信号与成交用实际 ES 合约、不用 SPY 代替 ES。本变更不违反它：策略轨仍只有 ES，SPY／QQQ 只进人工纪律轨。
