## ADDED Requirements

### Requirement: 账户限额按账户配置
系统 SHALL 从 `config/accounts.yaml` 读取每个账户的限额、费用和滑点预算，用于该账户的人工计划预检、`watch` 和成交导入。未登记的账户沿用 `config/risk.yaml` 的账户设置。账户限额 MUST NOT 影响纸面评测的风控版本和基线。

#### Scenario: 不改变纸面评测
- **WHEN** 修改某个账户的限额
- **THEN** `config/risk.yaml` 的内容和由它得出的 `risk_version` 不变

#### Scenario: 未批准的账户
- **WHEN** 账户的 `approved` 为 false
- **THEN** 它的人工计划预检结论为 `BLOCKED_BY_RISK`，原因 `BUDGET_NOT_APPROVED`
