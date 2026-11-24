## 1. 品种与限额

- [x] 1.1 `instruments.yaml` 登记换月规则 `none` 和 SPY、QQQ
- [x] 1.2 `manual/common.py`：股票类的合约解析
- [x] 1.3 `config/accounts.yaml` 与 `risk_for_account`，写入用户确认的限额
- [x] 1.4 计划、成交导入、快照、`watch` 改用新的解析；依赖策略的规则对股票类不评估

## 2. Alpaca

- [x] 2.1 `sources/alpaca_rest.py`：只读网络访问
- [x] 2.2 `manual/alpaca.py`：快照、成交行、订单事件行、导入
- [x] 2.3 命令行：`ingest --alpaca`，`watch` 和 `plan` 按账户选适配器

## 3. 测试与文档

- [x] 3.1 测试：品种、限额、计划、适配器、导入、watch、命令行、只读保证
- [x] 3.2 用 Alpaca 连接器取真实账户状态，跑一次 `watch --once` 和 `plan`
- [x] 3.3 文档：`docs/scan-plan-watch.md`、术语表、README、`CLAUDE.md`
- [x] 3.4 全部测试通过后提交并推送
