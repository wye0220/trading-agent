# Spec Delta

## Purpose

实现首版 Al Brooks 价格行为策略 PA v1.0：在 5 分钟线上识别尖峰及其回调，按固定规则给出信号、过滤原因和订单意图。只有 Spike 回调会生成订单意图。

## ADDED Requirements

### Requirement: Spike 的识别
策略 SHALL 把至少 2 根连续同向的强趋势 K 识别为 Spike，条件是首根开盘到末根收盘的方向幅度不小于 2.5 倍 ATR。强趋势 K 的实体不小于全幅的 50%，收盘位于方向一侧的 1/3 之内，并创出新的方向极值。策略 MUST 记录 Spike 的起止、幅度、极值、方向和所用的 ATR。

#### Scenario: 合格的牛 Spike
- **WHEN** 连续 2 根强阳线逐根创新高，首根开盘到末根收盘的涨幅等于 2.5 倍 ATR
- **THEN** 识别为牛 Spike，周期输出 `BULL_SPIKE`

#### Scenario: 幅度不足
- **WHEN** 连续 2 根强阳线的涨幅小于 2.5 倍 ATR
- **THEN** 不识别为 Spike

#### Scenario: 只有一根
- **WHEN** 只有 1 根强阳线，幅度超过 2.5 倍 ATR
- **THEN** 不识别为 Spike

#### Scenario: 熊 Spike 镜像
- **WHEN** 把合格牛 Spike 的 K 线按价格翻转
- **THEN** 识别为熊 Spike

### Requirement: 回调的开始与结束
Spike 之后第一根不再创新极值的 K 线 SHALL 记为回调第 1 根。回调期间某根的收盘越过 Spike 极值时，回调 SHALL 以 `TREND_RESUMED` 结束，之后不再延续 H／L 计数。

#### Scenario: 回调开始
- **WHEN** 牛 Spike 之后出现一根最高价不高于 Spike 极值的 K
- **THEN** 该 K 为回调第 1 根

#### Scenario: 趋势恢复
- **WHEN** 牛 Spike 回调中某根收盘高于 Spike 最高价
- **THEN** 回调以 `TREND_RESUMED` 结束，之后的 K 不再产生该 Spike 的 H 信号

### Requirement: 回调的失效
回撤比例为回调朝反方向走过的最大距离除以 Spike 首根开盘到 Spike 极值的距离。对 ES 和 MES，回撤超过 75% 或回调超过 20 根时回调 SHALL 作废；对 NQ 和 MNQ，阈值为 60% 和 12 根。等于阈值仍有效。达到失效的那一根 MUST NOT 发出信号。

#### Scenario: ES 回撤恰好 75%
- **WHEN** ES 的回调回撤恰好等于 75%
- **THEN** 回调仍有效

#### Scenario: ES 回撤超过 75%
- **WHEN** ES 的某根 K 使回撤超过 75%，而它本身满足 H 信号的形态
- **THEN** 回调作废，该 K 不发信号

#### Scenario: ES 第 21 根
- **WHEN** ES 的回调走到第 21 根
- **THEN** 回调作废

#### Scenario: NQ 用自己的阈值
- **WHEN** NQ 的回调回撤为 65%
- **THEN** 回调作废

### Requirement: H 与 L 的计数
牛 Spike 的回调中，最低价严格低于前一根最低价的 K 开启一条反向腿。腿开启之后、晚于腿首根的第一根收阳且收在上半部、并通过质量检查的 K SHALL 标为 H1；之后每条新腿对应的标为 H2、H3。每条腿只计一次，同一根 K MUST NOT 重复编号。熊 Spike 镜像，标为 L1、L2、L3。

#### Scenario: 第一个 H1
- **WHEN** 牛 Spike 回调中先有一根破前低的 K，随后一根收阳且收在上半部
- **THEN** 后一根标为 H1，`signal_status` 为 `SETUP_VALID`

#### Scenario: 腿首根自己不能当信号 K
- **WHEN** 开启反向腿的那根 K 自己收阳且收在上半部
- **THEN** 它不标为 H 信号

#### Scenario: 没有新腿不升级
- **WHEN** H1 之后没有出现新的破前低的 K，又来一根收阳的 K
- **THEN** 它不标为 H2

#### Scenario: 新腿之后是 H2
- **WHEN** H1 之后出现新的破前低的 K，随后一根合格的阳线
- **THEN** 它标为 H2

### Requirement: 信号 K 的质量
信号 K SHALL 满足：实体不小于全幅的 20%，全幅不超过 3 倍 ATR，全幅不为零。不满足时 MUST NOT 生成订单意图，并记录原因。

#### Scenario: 实体过小
- **WHEN** 候选信号 K 的实体为全幅的 15%
- **THEN** 不生成意图，原因指明实体不足

#### Scenario: 全幅过大
- **WHEN** 候选信号 K 的全幅超过 3 倍 ATR
- **THEN** 不生成意图，原因指明全幅过大

### Requirement: 订单意图的数值
Spike 回调的信号 SHALL 生成有效 1 根的 `STOP_LIMIT_NEXT_BAR` 意图。做多时触发价为信号 K 最高价加 1 tick，限价为触发价加配置的偏移（默认 1 tick），保护止损为回调最低价减 1 tick。做空镜像。

#### Scenario: 做多的数值
- **WHEN** H1 信号 K 最高 5000.00，回调最低 4990.00，ES 的 tick 为 0.25
- **THEN** 触发价 5000.25，限价 5000.50，保护止损 4989.75，有效根数 1

#### Scenario: 做空的数值
- **WHEN** L1 信号 K 最低 5000.00，回调最高 5010.00
- **THEN** 触发价 4999.75，限价 4999.50，保护止损 5010.25

### Requirement: 目标的选择
RTH 且 ADR 可用时，目标 SHALL 为 `OFFSET_FROM_FILL_TICKS`，数值是 0.40 倍 ADR 向上取整的整数 tick，至少为 1。ADR 不可用时，目标 SHALL 为 `INITIAL_RISK_MULTIPLE`，数值 2。决策元数据 MUST 记录选择依据和 ADR 的有效日数。

#### Scenario: ADR 可用
- **WHEN** ADR 为 201 tick
- **THEN** 目标为 `OFFSET_FROM_FILL_TICKS`，数值 81

#### Scenario: ADR 不可用
- **WHEN** ADR 的有效日少于 3 个
- **THEN** 目标为 `INITIAL_RISK_MULTIPLE`，数值 2

### Requirement: 只有已启用的形态生成订单
v1.0 中 SHALL 只有 Spike 回调生成订单意图。其余形态登记优先级和启用开关，默认全部关闭。在配置中启用一个尚未实现的形态 MUST 导致加载失败。

#### Scenario: 默认配置
- **WHEN** 加载 v1.0 的默认配置
- **THEN** 只有 Spike 回调处于启用状态

#### Scenario: 启用未实现的形态
- **WHEN** 配置把楔形设为启用
- **THEN** 加载失败并指出该形态尚未实现

### Requirement: 过滤器链
策略 SHALL 按固定顺序评估过滤器，记录每个过滤器的结论和第一个拦截原因。每个形态豁免哪些过滤器由配置登记。被拦截的信号 MUST 输出 `WAIT` 和拦截原因，不生成意图。

#### Scenario: Spike 回调的豁免
- **WHEN** 一个 Spike 回调的 H1 信号出现，而 1 小时方向未知
- **THEN** 它不因 H1 拦截、逆 1 小时方向、区间位置或衰竭过滤器被拦截

#### Scenario: 记录第一个拦截原因
- **WHEN** 一个候选信号同时触发两个过滤器
- **THEN** `skip_reason` 是顺序在前的那个，两者的证据都被记录

### Requirement: 尾盘不追单
信号 K 的收线时刻在 RTH 收盘前 30 分钟以内时，策略 SHALL 拦截顺势信号并给出原因。提前收盘日 MUST 按当日实际收盘时刻计算。

#### Scenario: 常规日 15:35 的信号
- **WHEN** 一个 Spike 回调信号 K 在 15:35 ET 收线
- **THEN** 输出 `WAIT`，原因为尾盘窗口

#### Scenario: 常规日 15:30 的信号
- **WHEN** 信号 K 在 15:30 ET 收线
- **THEN** 不因尾盘窗口被拦截

#### Scenario: 半日市
- **WHEN** 当日 13:00 ET 收盘，信号 K 在 12:40 ET 收线
- **THEN** 输出 `WAIT`，原因为尾盘窗口

### Requirement: 两次止损后停手
同一交易日内纸面交易连续两次止损出场后，策略 SHALL 在当日剩余时间不再输出 `SETUP_VALID`。达到两次之前，只有已完结的净盈利交易能把计数清零；亏损或保本出场 MUST NOT 清零。交易日切换时计数清零。

#### Scenario: 两次止损
- **WHEN** 当日已有两笔止损出场的纸面交易，随后出现合格的 H 信号
- **THEN** 输出 `WAIT`，原因为两次止损停手

#### Scenario: 中间有一笔盈利
- **WHEN** 当日依次为止损、净盈利、止损，随后出现合格的信号
- **THEN** 信号有效

#### Scenario: 保本不清零
- **WHEN** 当日依次为止损、保本出场、止损，随后出现合格的信号
- **THEN** 输出 `WAIT`，原因为两次止损停手

#### Scenario: 次日恢复
- **WHEN** 前一交易日停手，新的交易日出现合格的信号
- **THEN** 信号有效

### Requirement: 特征不可用时等待
所需特征不可用时，策略 SHALL 输出 `WAIT` 并指明缺少哪个特征，MUST NOT 用估计值代替。

#### Scenario: ATR 尚未预热
- **WHEN** ATR14 不可用
- **THEN** 输出 `WAIT`，原因指明缺少 ATR

### Requirement: 未定量的结构标为未知
1 小时方向、宽通道、交易区间和交易区间日在量化规则获批之前 SHALL 输出 `UNKNOWN`。`UNKNOWN` MUST NOT 被当作交易区间处理。

#### Scenario: 1 小时方向
- **WHEN** 使用 v1.0 的默认配置
- **THEN** 决策元数据中的 `htf_direction` 为 `UNKNOWN`

### Requirement: 决策输出
每次决策的元数据 SHALL 包含策略家族、合约、信号 K 结束时刻、`known_at`、周期、1 小时方向、形态、H／L 标签、方向、信号 K 质量、证据、`skip_reason`、下一根有效期、入场、止损、目标及目标规则。`setup_id` MUST 由策略家族、实际合约、形态、信号 K 时刻和版本稳定生成。

#### Scenario: setup_id 稳定
- **WHEN** 同一组输入运行两次
- **THEN** 两次的 `setup_id` 相同

#### Scenario: 不同信号 K
- **WHEN** 同一 Spike 的 H1 和 H2 先后出现
- **THEN** 它们的 `setup_id` 不同

### Requirement: 中文短报
策略 SHALL 能把决策渲染为中文：先给新信号及其状态，再给周期、最新 K 线、参考价和下一确认条件，末尾附固定的免责声明。

#### Scenario: 有信号
- **WHEN** 渲染一个 `SETUP_VALID` 的结果
- **THEN** 文本先出现信号的方向、标签、触发价、止损和目标，最后一行是免责声明

### Requirement: 人工计划一致性
计划方向与当前有效的 Spike 相反时，评估 SHALL 给出 `DEVIATES` 并标记 `COUNTERTREND_BEFORE_CONFIRMATION`。计划与当前有效信号方向一致时 SHALL 给出 `ALIGNED` 并附对应的 `setup_id`。计划没有止损时 MUST 标记 `NO_STOP`。

#### Scenario: 熊 Spike 中抄底
- **WHEN** 当前处于熊 Spike 或其回调中，计划做多
- **THEN** 结论为 `DEVIATES`，标记 `COUNTERTREND_BEFORE_CONFIRMATION`

#### Scenario: 与信号一致
- **WHEN** 当前有一个做多的有效信号，计划做多
- **THEN** 结论为 `ALIGNED`，附该信号的 `setup_id`

#### Scenario: 没有止损
- **WHEN** 计划没有填写止损
- **THEN** 标记中含 `NO_STOP`

### Requirement: 多空对称
策略对一串 K 线的判定 SHALL 与对其价格翻转后的 K 线的判定互为镜像：方向相反、H 与 L 互换、价位关于翻转轴对称。

#### Scenario: 镜像运行
- **WHEN** 把一个做多场景的全部价格关于某个价位翻转后运行
- **THEN** 每一根的信号状态相同，方向相反，触发价、止损关于该价位对称
