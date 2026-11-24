## ADDED Requirements

### Requirement: 截止日之后的实时行情放行
`config/live_data.yaml` 登记的来源、从登记首日起的交易日，即使晚于开发可见截止日，导入时 SHALL 进运行库，只供常驻纸面运行、影子和发布后监控使用。同一天其他来源的数据 SHALL 照旧封存。截止日清理 SHALL 不移出放行的数据。切分登记 SHALL 把放行日排除在所有段之外。放行首日不晚于已登记封存的最后一天时，该品种 SHALL 不放行，导入摘要写明冲突。

#### Scenario: 放行首日之后的实时来源
- **WHEN** 导入 Webull 在放行首日及以后的 K 线，以及 Databento 同一天的 K 线
- **THEN** Webull 的进运行库，Databento 的只记封存覆盖

#### Scenario: 与已封存日期冲突
- **WHEN** 放行首日不晚于已登记封存的最后一天
- **THEN** 该品种不放行，数据照旧封存，导入摘要列出冲突
