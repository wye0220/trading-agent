# agent-cli Specification

## Purpose

提供 `python -m trading_agent` 这一个命令行入口，按四维 scope 路由到各子命令，让交易员和定时任务用同一套命令操作系统。

## Requirements

### Requirement: 子命令清单
CLI SHALL 提供子命令 `ingest`、`collect`、`scan`、`plan`、`watch`、`replay`、`review`、`evolve`、`evaluate`、`shadow`、`release`、`rollback`、`freeze`、`monitor`。

#### Scenario: 列出子命令
- **WHEN** 用户运行 `python -m trading_agent --help`
- **THEN** 输出列出全部 14 个子命令

### Requirement: 未实现子命令的统一返回
尚未实现的子命令 SHALL 输出包含 `NOT_IMPLEMENTED` 和该子命令所属阶段的结构化结果，并以非零退出码结束，且 MUST NOT 读写任何业务数据。

#### Scenario: 调用未实现的子命令
- **WHEN** 用户运行 `python -m trading_agent replay`
- **THEN** 输出状态为 `NOT_IMPLEMENTED`，退出码非零，运行库内容不变

### Requirement: 四维 scope 参数
需要 scope 的子命令 SHALL 接受 `--scope <strategy_family>:<instrument_root>:<session_type>:<timeframe>`，缺省值为 `pa_brooks:ES:RTH:5m`。格式不合法或引用未登记品种的 scope MUST 被拒绝并说明原因。

#### Scenario: 使用默认 scope
- **WHEN** 用户运行 `scan` 且不带 `--scope`
- **THEN** 命令按 `pa_brooks:ES:RTH:5m` 执行，并在输出中回显该 scope

#### Scenario: scope 格式错误
- **WHEN** 用户传入 `--scope ES:5m`
- **THEN** 命令报告 scope 格式错误，以非零退出码结束，不执行任何操作

#### Scenario: 品种未登记
- **WHEN** 用户传入 `--scope pa_brooks:CL:RTH:5m`，而 `CL` 不在合约规格表中
- **THEN** 命令报告品种未登记，以非零退出码结束

### Requirement: 机器可读输出
`ingest`、`collect` 和 `scan` SHALL 支持 JSON 输出，退出码 0 表示成功、非零表示失败，使定时任务无须解析中文文本就能判断结果。

#### Scenario: 导入成功
- **WHEN** `ingest --json` 成功完成
- **THEN** 标准输出是一个合法 JSON 对象，含运行 ID、处理的文件数和各类计数，退出码为 0

### Requirement: plan 与 watch 子命令可用
CLI 的 `plan` SHALL 支持登记并预检计划（含一行快捷语法）、激活、取消和列出计划。`watch` SHALL 支持单次评估和循环运行，并接受轮询间隔参数。两者 SHALL 支持 JSON 输出。

#### Scenario: 预检并输出 JSON
- **WHEN** 运行 `plan ES L 5000 4995 5010 1 test --json`
- **THEN** 标准输出是一个合法 JSON 对象，含 `plan_id`、结论和解析出的实际合约

#### Scenario: 单次评估
- **WHEN** 运行 `watch --once`
- **THEN** 命令评估一次后退出

### Requirement: ingest 可导入人工成交
`ingest` SHALL 接受人工成交文件和订单事件文件作为参数，导入它们而不扫描行情目录。

#### Scenario: 导入成交文件
- **WHEN** 以成交文件为参数运行 `ingest`
- **THEN** 输出导入、跳过和隔离的条数，行情数据不变

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

### Requirement: review 子命令
`review` SHALL 支持：`--day YYYY-MM-DD`（单日）、`--from`／`--to`（区间）、`--run-id`（缺省取该 scope 已冻结基线的主运行）、`--account`（带上人工轨）、`--seal`（复盘前先封存）。输出每个 session 的结论摘要和报告路径。

#### Scenario: 没有可用的运行
- **WHEN** 没有给 `--run-id`，该 scope 也没有已冻结的基线
- **THEN** 命令以配置错误退出

### Requirement: evolve 子命令
CLI SHALL 提供 `evolve`：默认做聚合并生成候选；`--last N` 指定窗口；`--import-proposals FILE` 导入提案；`--estimate` 为排队候选估计效应；`--pre-eval-shadow` 运行前向预演；`--ledger` 只显示台账统计。输出 JSON 或中文短报。

#### Scenario: 默认运行
- **WHEN** 运行 `evolve`
- **THEN** 输出聚类数、可升级数、新候选和队列，并写出周报

#### Scenario: 没有已冻结的基线
- **WHEN** scope 没有已冻结的基线
- **THEN** 命令拒绝，错误码 `NO_BASELINE`

### Requirement: memory 子命令
CLI SHALL 提供 `memory`：默认列出头指针的 `memory_version`、生效条目的句子和待处理的候选；`--confirm ID`、`--reject ID`、`--supersede ITEM_ID`（可带 `--evidence`）、`--rollback MEMORY_VERSION`、`--history`、`--context KIND`。输出 JSON 或中文短报。

#### Scenario: 确认偏好候选
- **WHEN** 运行 `memory --confirm <候选标识>`，候选是合法的 `user_preference`
- **THEN** 输出新的 `memory_version` 和生成的句子，退出码 0

#### Scenario: 候选不合法
- **WHEN** 被确认的候选没通过校验
- **THEN** 命令拒绝并给出原因码，退出码 1，不改任何状态

### Requirement: evaluate 子命令
CLI SHALL 提供 `evaluate`：`--register`、`--screen`、`--confirm`、`--labels`、`--status`、`--regression`、`--incident-list`、`--incident-confirm`、`--isolation-check`、`--digests`、`--accept-digests`。输出 JSON 或中文短报。

#### Scenario: 查看状态
- **WHEN** 运行 `evaluate --status`
- **THEN** 列出登记、各阶段状态、窗口占用和余下的 α
