## Why

阶段 6A 每天产出带机制标签的观察，但没有任何东西把它们跨日合起来、决定哪些值得变成可评测的改动。阶段 6B 补上这一环：确定性聚合、升级规则、候选结构、候选台账和双轨队列，为阶段 8 的独立评测准备输入。

## What Changes

- 新增 `evolve` 子命令：只读最近 N 个已封存并已复盘的 session、候选目录和开发可见数据。
- Map：按 scope × 机制标签 ×（原因）× 配置差异键做确定性聚类，统计出现天数、次数、反例天数和入场时段集中度，各留最多 3 条正例和 3 条反例。
- 升级规则：跨 `min_days`（默认 3）个不同交易日才可升为 `PROPOSED`，否则只是 `OBSERVATION`。
- 候选来源两种，走同一套校验：预登记的模板（`config/evolve.yaml`）和导入的模型提案（`--import-proposals`）。项目目前没有模型调用通道，Reduce 一步只做到"把每个聚类的摘要放进解释队列"加"导入提案文件"。
- 候选约束：每个策略家族每次最多 3 个，每个只改一个键；差异必须落在策略登记的可改键白名单内，`from` 必须等于现行配置。
- 候选台账：创建、状态变更、开发尝试、正式检验登记与消耗都追加记录；按 scope × 假设家族统计余下的 α 额度。FAIL 过的机制重新提出时必须引用失败报告并说明差异。
- 队列：效应估计（开发段加向前验证段的配对日差）× 证据天数权重 × 机制可信度，公式和权重预登记在配置里。超过 `queue_ttl_days` 或基准被替换的候选转 `SUPERSEDED`。
- 队列前向预演：只做可运行性检查，并登记已观察的日期。截止日之后没有可用于预演的数据时返回 `NO_ELIGIBLE_DAYS`。
- 跨品种：同家族、同机制、同差异键出现在多个品种时生成候选组，各 scope 分别评测。
- `Candidate` schema 的 `exact_diff.from/to` 允许标量数组（过滤器豁免列表是数组）。

## Capabilities

### New Capabilities
- `evolve-aggregation`: 观察的跨日聚类、反例、升级规则和 Reduce 输入。
- `candidate-ledger`: 候选的结构、校验、落盘、状态变更台账和 α 额度统计。
- `candidate-queue`: 双轨分流、效应估计、排序、过期和前向预演。

### Modified Capabilities
- `agent-cli`: 新增 `evolve` 子命令。
- `runtime-storage`: 新增 migration `0006_evolve.sql`。

## Impact

- 新代码 `src/trading_agent/evolve/`，新配置 `config/evolve.yaml`，新 migration，`cli.py` 接入。
- 不改 `strategies/`、`engine/` 等基线代码摘要覆盖的文件，基线 `bl_6027564e58b04712` 不失效。
- 不读保留集和胶囊：策略轨 session 的交易日必须不晚于开发可见截止日。
- 效应估计要回放候选配置（每个约几分钟），计入台账的开发尝试。
