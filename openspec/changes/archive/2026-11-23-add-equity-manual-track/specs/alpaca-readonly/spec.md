## ADDED Requirements

### Requirement: 只读
Alpaca 适配器 SHALL 只读取持仓、挂单、订单记录和成交。网络访问 MUST 只发 GET 请求、只连模拟盘地址；代码中 MUST NOT 出现下单、改单、撤单或平仓的调用，测试须断言这一点。人工轨模块本身不含网络代码。

#### Scenario: 源码检查
- **WHEN** 检查适配器的源码
- **THEN** 只有一处网络请求，方法为 GET，主机为模拟盘地址，端点都是读取类

### Requirement: 两种取数方式
适配器 SHALL 在有 API 密钥时直接读取，否则读取保存在 `<data_root>/broker/<账户>/alpaca/` 下的 JSON 文件。两种方式的解析结果相同。密钥 MUST NOT 被打印或写入日志。

#### Scenario: 没有密钥也没有文件
- **WHEN** 两者都没有
- **THEN** 读取失败并说明缺哪个文件，不评估任何规则

### Requirement: 只处理已登记的品种
账户里未登记的代码和碎股 SHALL 被忽略并计数，不进入快照和账本。

#### Scenario: 账户里有其他股票
- **WHEN** 账户里同时有 SPY 和 AAPL 的持仓
- **THEN** 快照只含 SPY，结果里报告忽略了 AAPL

### Requirement: 成交导入
`ingest --alpaca` SHALL 把订单记录和成交转成与 CSV 导入相同的行，走同一套去重、开平判定、计划关联和纪律判定。成交价按最近的 tick 取整。订单的 `client_order_id` 里含有计划编号时，该成交 SHALL 以此作为与计划关联的凭据；否则只按时间吻合记为待核对。

#### Scenario: 重复导入
- **WHEN** 同样的成交导入两次
- **THEN** 第二次全部跳过

#### Scenario: 订单带计划编号
- **WHEN** 订单的 `client_order_id` 含有一份事前已激活计划的编号
- **THEN** 该开仓成交的关联状态为 `LINKED`
