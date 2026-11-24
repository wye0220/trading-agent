## ADDED Requirements

### Requirement: 回放复用引擎
`replay` SHALL 通过阶段 3 的 `runner.replay` 和注册表里的 `StrategyProvider` 运行，研究模块中 MUST NOT 出现另一套信号、风控或成交逻辑。

#### Scenario: 研究模块不含成交与信号逻辑
- **WHEN** 检查 `src/trading_agent/research/` 的源码
- **THEN** 其中没有调用 `fills.apply_bar`、没有引用任何策略家族的模块，回放只经由 `runner.replay`

#### Scenario: 真实数据的边界
- **WHEN** 回放区间包含真实数据，且区间晚于开发可见截止日或没有设置截止日
- **THEN** 命令拒绝运行并说明原因，不写任何账本

### Requirement: 基线冻结清单
系统 SHALL 按四维 scope 冻结基线，清单包含 `strategy_family`、策略版本与配置摘要、已启用形态、指标定义、提示词摘要、行情清单（逐日的来源、实际合约和含 revision 的内容摘要）、时段、费用与滑点、引擎版本、风控版本、随机种子，以及决定回放结果的源文件的摘要。`baseline_id` SHALL 由这些内容的摘要得出。git commit 和工作区是否干净 SHALL 随清单保存，但不参与 `baseline_id`。

#### Scenario: 相同输入得到相同标识
- **WHEN** 对同一份数据、配置和代码两次生成清单
- **THEN** 两次的 `baseline_id` 相同

#### Scenario: 行情变化改变标识
- **WHEN** 区间内任一根 K 线出现新的 revision
- **THEN** 行情清单的摘要和 `baseline_id` 都改变

#### Scenario: 工作区有未提交改动
- **WHEN** 冻结时 git 工作区不干净
- **THEN** 基线带 `DIRTY_WORKTREE` 标记

#### Scenario: 策略家族各自建档
- **WHEN** 为另一个策略家族的 scope 冻结基线
- **THEN** 它得到独立的 `baseline_id` 和运行，报告不与其他家族合并计算

### Requirement: 两次运行完全一致
冻结基线时系统 SHALL 用同样的输入回放两次，并比较逐笔交易、逐张订单和逐条决策记录的摘要。不一致时基线状态 MUST 为 `NON_DETERMINISTIC`，不得作为比较基准。

#### Scenario: 一致
- **WHEN** 两次回放的交易、订单和决策摘要全部相同
- **THEN** 基线状态为 `FROZEN`，清单记下三个摘要

#### Scenario: 不一致
- **WHEN** 任一摘要不同
- **THEN** 基线状态为 `NON_DETERMINISTIC`，命令以非零状态退出

### Requirement: 基线报告
系统 SHALL 生成 `reports/baseline_<scope>.md` 和同名 JSON，按切分段（开发、向前验证）和合计分别给出：
- 机会与成交：机会数、数据覆盖率、无交易日、订单数、成交数、未触发撤单数。
- 收益与成本：费用、滑点成本、毛与净盈亏、净 R／笔、净 R／日、胜率、PF。
- 风险：最大回撤、MAE／MFE、持仓时长、`AMBIGUOUS_BAR` 占比、跳空超计划损失。
- 分布：等待或被拒原因、按 `decision_metadata` 的分组、品种、时段、合约，以及只有 5 分钟子 K 的成交。
- 固定手数与风险规模两套结果。
- 估计可用时点的占比和所用的延迟假设。

#### Scenario: PF 分母为零
- **WHEN** 某一段没有亏损交易
- **THEN** PF 写"不可估"，不写无穷大或 0

#### Scenario: 分组由策略元数据驱动
- **WHEN** 生成按元数据的分组
- **THEN** 分组键来自 `config/research.yaml` 登记的元数据路径，报告代码不含任何策略专有的字段名

#### Scenario: 风险规模未获批
- **WHEN** 账户限额未配置或未批准
- **THEN** 风险规模一栏写"未配置"，固定手数结果照常给出

#### Scenario: 没有真实数据
- **WHEN** 回放的数据全部是合成数据
- **THEN** 报告带 `FIXTURE_ONLY`，策略有效性为 `EVIDENCE_INSUFFICIENT`，并列出真实行情在字段、频率、时段和覆盖年限上的缺口

### Requirement: 跨窗口持仓与预热的预登记
指标预热 SHALL 只使用回放起点之前已可见的数据。持仓 MUST NOT 跨交易日，因此不会跨切分窗口；这一点在清单中登记。

#### Scenario: 段首日
- **WHEN** 回放从某一段的第一个交易日开始
- **THEN** 当日的特征只来自此前的 K 线，开盘时没有持仓和挂单
