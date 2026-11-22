# Spec Delta

## Purpose

定期从 Webull 只读取得实际合约已收线的 K 线并保存为 CSV，让历史文件之后的每日行情持续进入系统，同时如实记录每根线是何时取到的。

## ADDED Requirements

### Requirement: 采集已收线的 K 线
`collect` SHALL 向 Webull 请求指定 scope 当前实际合约的 5 分钟线，并只保存在取数时刻已经收线的 K 线。尚在形成中的 K 线 MUST NOT 被保存。

#### Scenario: 收线后采集
- **WHEN** 在 09:35:10 ET 运行 `collect`
- **THEN** 09:30 开始的那根 5 分钟线被保存，09:35 开始的那根不被保存

#### Scenario: 包含夜盘
- **WHEN** 在夜盘时段运行 `collect`
- **THEN** 已收线的 ETH K 线被保存，`session_type` 为 ETH

### Requirement: 保存为按交易日分文件的 CSV
采集结果 SHALL 追加到数据根目录下按来源、品种、实际合约和交易日组织的 CSV 文件，列符合 K 线数据合同并含取数时刻。已有的行 MUST NOT 被改写或删除。

#### Scenario: 同一交易日多次采集
- **WHEN** 同一交易日内运行了多次 `collect`
- **THEN** 当日 CSV 只增加此前没有的行，原有各行逐字节不变

#### Scenario: 重复采集没有新线
- **WHEN** 两次运行之间没有新的 K 线收线
- **THEN** 第二次运行不向 CSV 写入任何行

### Requirement: 记录真实的可用时点
采集到的 K 线 SHALL 以它首次被取到的时刻作为 `available_at_utc`，且 `available_at_estimated=false`。事后补回的 K 线 MUST 使用补回时的取数时刻，不得倒填为收线时刻。

#### Scenario: 按时采集
- **WHEN** 09:30 的 K 线在 09:35:10 被首次取到
- **THEN** 它的 `available_at_utc` 为 09:35:10，估计标记为假

#### Scenario: 停机后补回
- **WHEN** 10:00 的 K 线因停机到 11:20 才被首次取到
- **THEN** 它的 `available_at_utc` 为 11:20 的取数时刻

### Requirement: 自动补缺
每次运行 SHALL 请求一段回看窗口内的 K 线，把其中尚未保存的补上。超出来源可回看范围的缺失 MUST 保留为缺口，不得用其他数据填充。

#### Scenario: 漏跑三次
- **WHEN** 连续三次调度没有运行，之后恢复
- **THEN** 恢复后的第一次运行补齐这段时间内已收线的 K 线

#### Scenario: 缺失超出回看范围
- **WHEN** 缺失的时段早于 Webull 能提供的最早 K 线
- **THEN** 该时段没有任何行被写入，后续校验把它报告为缺口

### Requirement: 修订作为新行保存
已保存的 K 线再次取到且内容不同时，系统 SHALL 追加一行带新取数时刻的记录，使它在导入后成为该 K 线的新 revision。

#### Scenario: 来源修正了成交量
- **WHEN** 已保存的某根 K 线再次取到时成交量不同
- **THEN** CSV 新增一行，导入后该 K 线有两个 revision，较早的保持不变

### Requirement: 使用实际合约
`collect` SHALL 按版本化的换月规则确定要请求的实际合约。来源返回的合约与预期不符时，本次运行 MUST NOT 保存任何行。系统 MUST NOT 用其他品种或 ETF 代替期货合约。

#### Scenario: 换月日之后
- **WHEN** 换月规则指定当日实际合约为 `ESH2027`
- **THEN** `collect` 请求并保存的是 `ESH2027` 的 K 线

#### Scenario: 返回了别的合约
- **WHEN** 来源返回的数据属于另一个合约
- **THEN** 本次不写入任何行，记录一条告警，退出码非零

### Requirement: 失败时不产生数据
请求失败、凭证缺失、无行情权限或返回为空时，`collect` SHALL 不写入任何行，记录原因，并以非零退出码结束。

#### Scenario: 没有行情订阅
- **WHEN** Webull 返回没有该品种行情权限的错误
- **THEN** CSV 不变，输出说明原因，退出码非零

#### Scenario: 凭证未配置
- **WHEN** 环境中没有 Webull 凭证
- **THEN** 命令不发起请求，提示缺少凭证，退出码非零

### Requirement: 只读
采集功能 MUST 只调用行情读取接口。代码中 MUST NOT 出现下单、改单、撤单或任何交易接口的调用。

#### Scenario: 静态检查
- **WHEN** 运行测试套件
- **THEN** 有测试断言源码中不引用 Webull 的交易模块，也不出现下单、改单、撤单的调用

### Requirement: 凭证不外泄
Webull 凭证 SHALL 只从环境变量或不进版本控制的本地文件读取，MUST NOT 出现在日志、标准输出、CSV、数据库或报告中。

#### Scenario: 采集后检查输出
- **WHEN** 完成一次采集
- **THEN** 标准输出、日志文件和 CSV 中都不含凭证内容

### Requirement: 采集后入库
`collect` 成功写入新行后 SHALL 触发增量导入，使新 K 线在同一次运行结束时可被 `scan` 使用。

#### Scenario: 采集到新线
- **WHEN** `collect` 保存了一根新 K 线并正常结束
- **THEN** 随后运行的 `scan` 能看到这根 K 线
