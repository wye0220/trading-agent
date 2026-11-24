## ADDED Requirements

### Requirement: 事故草稿的触发
复盘 SHALL 为当日的 `DATA_ERROR`、引擎异常（无法定价、成交后失效、运行器错误告警）和重大成交歧义（`AMBIGUOUS_BAR`、`GAP_THROUGH_STOP`）各生成一份事故草稿。草稿状态为 `DRAFT`，要等用户确认才进入回归库。

#### Scenario: 普通歧义不立案
- **WHEN** 当日只有 `AMBIGUOUS_ENTRY`
- **THEN** 不生成事故草稿，只在策略轨里计数

### Requirement: 草稿内容
事故草稿 SHALL 保存：事故前的可恢复检查点（策略状态、版本元组、水位、风险域、当时的仓位和挂单）、从检查点到事故处理后的原始事件、所依赖行情的 revision 清单，以及预期与实际行为。检查点默认取事故前 48 小时内最早的一条决策记录。缺失的 K 线和断流 MUST 原样记录，不得填补。

#### Scenario: 缺数据
- **WHEN** 事故窗口内有缺口
- **THEN** 草稿列出缺口区间，行情清单里没有补出来的 K 线

#### Scenario: 重复生成
- **WHEN** 同一个 session 再次复盘
- **THEN** 同一事故得到同一个草稿标识，不重复登记
