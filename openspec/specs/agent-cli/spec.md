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

### Requirement: 最小 scan 只做数据校验
阶段 1 的 `scan` SHALL 对指定 scope 截至 `as_of_utc` 已可见的数据运行校验，输出 JSON 和中文短报，其中 `signal_status` 只取 `WAIT` 或 `DATA_ERROR`。它 MUST NOT 产生交易信号、纸面订单或成交。

#### Scenario: 数据正常
- **WHEN** 对一段完整无异常的数据运行 `scan`
- **THEN** 输出 `signal_status=WAIT`，并附快照 ID、实际合约、最新 bar 的完成时刻与可用时刻

#### Scenario: 数据异常
- **WHEN** 对含有缺失 bar 的数据运行 `scan`
- **THEN** 输出 `signal_status=DATA_ERROR`，列出异常类别和受影响的时间段

### Requirement: 机器可读输出
`ingest`、`collect` 和 `scan` SHALL 支持 JSON 输出，退出码 0 表示成功、非零表示失败，使定时任务无须解析中文文本就能判断结果。

#### Scenario: 导入成功
- **WHEN** `ingest --json` 成功完成
- **THEN** 标准输出是一个合法 JSON 对象，含运行 ID、处理的文件数和各类计数，退出码为 0
