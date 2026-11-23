## ADDED Requirements

### Requirement: 主指标与置信界
主指标 SHALL 是窗口内每个有效交易日的候选净 R 减基线净 R，零交易日计入。置信界 SHALL 用预登记的区块长度、次数和种子做连续交易日区块 bootstrap，按分配到的单侧 α 取。尾部过稀或有效日太少时 SHALL 报 `EVIDENCE_INSUFFICIENT`。

#### Scenario: 结果可复现
- **WHEN** 同样的两条逐日序列计算两次
- **THEN** 置信界完全相同

#### Scenario: 不可估
- **WHEN** 有效日少于两个区块
- **THEN** 置信界为空，结论 `EVIDENCE_INSUFFICIENT`

### Requirement: Track A 与 Track B 门槛
Track A SHALL 要求置信下界大于负的非劣效界，且最大回撤改善达到预登记幅度。Track B SHALL 要求平均配对提升不小于 δ、置信下界大于 0、回撤增量不超过预登记范围。两者 SHALL 都要求有效日和每臂成交数达到预登记下限、加滑点压力后候选净结果为正且不比基线差、回归零失败。

#### Scenario: 低频
- **WHEN** 确认窗内任一臂成交数不到下限
- **THEN** 结论 `EVIDENCE_INSUFFICIENT`，不是 `FAIL`

#### Scenario: 回归失败
- **WHEN** 事故回归或合成回归有任何一项失败
- **THEN** 结论 `FAIL`，不解封窗口

#### Scenario: Track B 通过
- **WHEN** 样本足够、平均提升不小于 δ、置信下界大于 0、回撤增量在范围内、压力后为正且不退化
- **THEN** 结论 `PASS_FOR_SHADOW`

### Requirement: 差异交易检验只作辅助
对 Track A，评测 SHALL 单列"基线入场但候选拦截"的交易及其平均净 R 的单侧置信上界，并报告候选独有的交易数。它 SHALL NOT 改变结论。

#### Scenario: 路径差异
- **WHEN** 候选出现了基线没有的交易
- **THEN** 报告里列出数量，结论仍由完整回放的配对日差决定

### Requirement: 标注类评测
解释／纪律类和 Memory 类候选 SHALL 用人工标注的识别率、误报率、引用正确率和最低样本数评测，并要求回归通过、代码摘要与基线一致。

#### Scenario: 样本不足
- **WHEN** 标注样本少于下限
- **THEN** 结论 `EVIDENCE_INSUFFICIENT`

#### Scenario: 通过
- **WHEN** 三项指标都达到阈值
- **THEN** 写入状态 `PASSED` 的评测记录
