## Context

用户在 Alpaca 模拟盘（权益约 98,000 美元）交易 SPY、QQQ。人工轨原先只认期货。基线刚冻结，`config.py`、`contracts.py` 属于决定回放结果的代码，改它们会让基线失效。

## Goals / Non-Goals

**Goals**：SPY、QQQ 能登记计划、导入成交、被 `watch` 监看；限额按账户；全程只读。不让基线失效。

**Non-Goals**：不为 SPY、QQQ 做策略信号、回放、基线和行情采集；不接下单。

## Decisions

1. **股票类用"换月规则种类为 none"表示，不给品种加新字段。** `instruments.yaml` 里 `months: []`、`roll_rule_version: none`。品种与合约的解析放在 `manual/common.py`（`is_equity`、`active_contract`、`resolve_contract`、`instrument_of`）。这样不用改 `config.py` 和 `contracts.py`，基线保持有效。代价是"品种类型"没有显式字段；等下次有理由动那两个文件时再补。
2. **账户限额独立成 `config/accounts.yaml`。** `risk_for_account` 在 `risk.yaml` 的基础上换掉账户限额、费用和滑点预算。`CLAUDE.md` 规定纸面评测不受账户预算影响，所以限额不该进 `risk_version`。
3. **策略一致性对股票类一律 `UNKNOWN`。** 预检结论沿用既有规则（无法确认一致时为 `DEVIATES`），不新增枚举值；摘要里写明"该品种没有正式策略，不评价"。`watch` 里依赖策略状态的规则对股票持仓不评估。
4. **Alpaca 适配器分两层。** 网络访问在 `sources/alpaca_rest.py`（只有 GET、只连模拟盘）；解析在 `manual/alpaca.py`，没有网络代码，既有的"人工轨模块不含网络调用"的测试继续成立。没有密钥时读保存的 JSON 文件，会话里用 Alpaca 连接器取回后存下即可。
5. **成交导入复用 CSV 导入函数。** 适配器把成交和订单记录写成 CSV 再调用既有函数，去重、先进先出、计划关联、纪律判定都不重写。
6. **计划关联的凭据是订单的 `client_order_id`。** 里面含计划编号就算可证明；否则按既有规则记为待核对并给出建议的计划。
7. **价格取整。** Alpaca 的成交价和均价可能带到小数点后四位，按最近的一分钱取整。行情数据的"不四舍五入"规则针对的是 K 线；人工成交的记账允许这个精度损失，每股最多半分钱。
8. **登记或激活计划前先读一次账户。** 预检要求账户状态足够新。只对登记为 Alpaca 的账户做，读不到不报错，由预检如实报告状态未知。

## Risks / Trade-offs

- 股票的盘后成交按期货的交易日切分（18:00 ET）归日 → 18:00 之后的盘后成交会记到下一个交易日。
- Alpaca 里改止损是"替换成一张新订单"，订单号会变；目前只记录提交、撤销、成交、到期四种事件，所以"放宽止损"这类判定在 Alpaca 上多半是 `UNKNOWN`。
- 做空在 Alpaca 的成交方向是 `sell_short`，按卖出处理。

## Migration Plan

只加配置和代码，不改表结构。回退：从 `instruments.yaml` 去掉 SPY、QQQ，删掉 `accounts.yaml`。
