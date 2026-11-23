# Spec Delta

## ADDED Requirements

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

## REMOVED Requirements

### Requirement: 最小 scan 只做数据校验
**Reason**: `scan` 现在在数据校验之后运行策略和纸面账本，由 `paper-scan` 规格描述。
**Migration**: 需要只做数据校验时，对真实数据使用带决策时点的历史扫描（未设开发可见截止日时它只返回数据校验结果）；程序里可直接调用数据校验函数。
