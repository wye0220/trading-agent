# strategy-plugin-spi Specification

## Purpose

定义所有策略共用的插件接口和标准订单意图，使引擎、扫描器和回放不必了解任何一个策略的内部细节，新增策略时也不用改它们。

## Requirements

### Requirement: 统一的策略接口
每个策略 SHALL 提供策略家族名、版本、所需特征的声明、初始状态、逐 bar 决策和人工计划一致性评估。逐 bar 决策 MUST 返回信号状态、订单意图（可为空）、决策元数据和下一轮状态。

#### Scenario: 调用一个策略
- **WHEN** 以特征上下文和上一轮状态调用策略的逐 bar 决策
- **THEN** 返回结果含 `signal_status`（`WAIT`、`SETUP_VALID` 或 `DATA_ERROR`）、订单意图或空、决策元数据和下一轮状态

#### Scenario: 没有信号时不带订单意图
- **WHEN** 结果的 `signal_status` 不是 `SETUP_VALID`
- **THEN** 订单意图为空

### Requirement: 策略是无副作用的纯函数
策略 MUST NOT 读写文件、数据库或网络，MUST NOT 修改传入的上下文和状态。相同的上下文、状态和配置 MUST 得到相同的结果。

#### Scenario: 重复调用
- **WHEN** 用同样的上下文和状态调用两次
- **THEN** 两次的结果完全相同，传入的状态对象没有变化

#### Scenario: 策略模块不做 I/O
- **WHEN** 运行测试套件
- **THEN** 有测试断言策略模块不引用数据库、文件写入和网络库

### Requirement: 状态可序列化
策略状态 SHALL 带状态结构版本号，并能序列化为只含 JSON 基本类型的文本。从序列化文本恢复后继续运行 MUST 与不经序列化直接运行得到相同的结果。

#### Scenario: 中途保存再恢复
- **WHEN** 一串 K 线跑到一半时把状态序列化再反序列化，然后继续跑完
- **THEN** 之后每一根的结果与不中断时相同

### Requirement: 标准订单意图
订单意图 SHALL 包含方向、订单类型、触发价、限价（适用时）、有效根数、保护止损、目标描述、`setup_id` 和证据。价格 MUST 是整数 tick。订单类型只能是 `STOP_LIMIT_NEXT_BAR`、`MARKET_NEXT_OPEN`、`LIMIT_RETRACE`。

#### Scenario: 合法的做多止损限价意图
- **WHEN** 构造触发价 20001、限价 20002、止损 19980 的做多 `STOP_LIMIT_NEXT_BAR` 意图
- **THEN** 构造成功

#### Scenario: 止损在错误的一侧
- **WHEN** 构造做多意图而保护止损不低于触发价
- **THEN** 构造被拒绝并说明原因

#### Scenario: 限价在错误的一侧
- **WHEN** 构造做多的 `STOP_LIMIT_NEXT_BAR` 意图而限价低于触发价
- **THEN** 构造被拒绝并说明原因

### Requirement: 目标只能用三种通用描述
订单意图的目标 SHALL 是 `ABSOLUTE_PRICE_TICKS`、`OFFSET_FROM_FILL_TICKS` 或 `INITIAL_RISK_MULTIPLE` 之一，各带一个正数值。意图中 MUST NOT 含有可执行代码，或靠策略名决定成交方式的字段。

#### Scenario: 未知的目标类型
- **WHEN** 构造目标类型为 `ADR_FRACTION` 的意图
- **THEN** 构造被拒绝

#### Scenario: 目标数值不为正
- **WHEN** 构造 `OFFSET_FROM_FILL_TICKS` 为 0 的目标
- **THEN** 构造被拒绝

### Requirement: 策略注册表
系统 SHALL 提供按策略家族名注册和查找策略的注册表。同名重复注册和查找未注册的家族 MUST 报错。

#### Scenario: 查找已注册的策略
- **WHEN** 按 `pa_brooks` 查找
- **THEN** 得到 PA 策略的实例，它的家族名为 `pa_brooks`

#### Scenario: 查找未注册的策略
- **WHEN** 按 `orb` 查找而它没有注册
- **THEN** 报错并列出已注册的家族

### Requirement: 接口不依赖具体策略
接口的类型和注册表 MUST NOT 引用任何具体策略的内部字段。一个与 PA 无关的策略 SHALL 能只靠这套接口被调用。

#### Scenario: 用模拟策略走通接口
- **WHEN** 注册一个每根都返回 `MARKET_NEXT_OPEN` 意图和 `INITIAL_RISK_MULTIPLE` 目标的模拟策略，并逐 bar 调用
- **THEN** 调用方不需要任何 PA 专有的字段就能取得意图、元数据和状态

### Requirement: 人工计划一致性评估
策略 SHALL 能对一份人工交易计划给出 `ALIGNED`、`DEVIATES` 或 `UNKNOWN` 的结论，并附标记和证据。

#### Scenario: 没有可判断的结构
- **WHEN** 策略当前没有任何有效的结构状态
- **THEN** 结论为 `UNKNOWN`
