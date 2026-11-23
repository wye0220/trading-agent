# Tasks

## 1. 准备与存储

- [x] 1.1 确认在分支 `phase-4-scan-plan-watch`；验证：`git branch --show-current`
- [x] 1.2 写 `config/manual.yaml`（默认账户、计划默认有效分钟数、止损后冷却、静默窗口、覆盖率阈值、止损容差、各规则开关）和 `config/news_windows.yaml`（默认为空），并实现加载与校验；验证：默认值和非法值的单元测试
- [x] 1.3 写运行库 migration `0003_manual.sql`；验证：在已有前两个 migration 的库上应用后新表和新列存在、原有数据不变；阶段 1–3 的全部测试仍通过
- [x] 1.4 在 `docs/data-contract.md` 的"存储"一节补上新表；验证：新表各有一行说明

## 2. 计划与预检

- [x] 2.1 实现快捷语法解析：根代码解析为实际合约、方向、价格（须在 tick 上）、手数、理由，`-` 表示没有；验证：正确解析、字段缺失、价格不在 tick 上、未登记品种各一个测试
- [x] 2.2 实现计划登记与状态机（`DRAFT`、`ACTIVE`、`PARTIALLY_FILLED`、`FILLED`、`CANCELLED`、`EXPIRED`），带版本校验和 `plan_events` 审计；验证：登记→激活→取消的审计轨迹、过期版本被拒
- [x] 2.3 实现预检：策略一致性（取纸面运行的最新状态，过期或没有则 `UNKNOWN`）、账户风险、纪律标记、合成结论，结果通过 `PretradeCheck` schema；验证：逆势得 `DEVIATES` 和标记、限额未批准得 `BLOCKED_BY_RISK`、无策略状态得 `UNKNOWN` 加 `DEVIATES`、无止损标 `NO_STOP`、与有效信号同向得 `ALIGNED`
- [x] 2.4 实现 `RETRY_WITHOUT_NEW_SETUP`；验证：止损后同向再来被标记，换方向或中间有盈利不标记
- [x] 2.5 实现激活（重新预检，同一事务预留）、取消、过期（释放预留）；验证：激活成功预留增加、被拦截时保持 `DRAFT`、过期和取消后预留归零

## 3. 成交导入

- [x] 3.1 实现成交 CSV 的读取、必需列校验、幂等（成交号或合成键）和隔离；验证：合法文件、缺列、重复导入不变、两行相同无成交号被隔离
- [x] 3.2 实现开平判定（含跨零拆分）和先进先出的已实现盈亏；验证：反手拆成平 1 开 1、盈亏与手算一致
- [x] 3.3 实现订单事件 CSV 的导入；验证：修改事件含新价格和时间，重复导入不变
- [x] 3.4 把平仓登记到账户风险记录（止损类型计止损次数，按成交号去重）；验证：止损出场次数加一、无订单事件时只记亏损、重复导入不重复计数
- [x] 3.5 实现计划关联的四种情形和计划成交状态的更新；验证：`LINKED`、`UNKNOWN` 加建议、`UNPLANNED`、事后补录、分批成交到 `FILLED` 并释放预留

## 4. 纪律事件与 KPI

- [x] 4.1 实现纪律事件的生成：`UNPLANNED`、`COUNTERTREND_BEFORE_CONFIRMATION`、`RETRY_WITHOUT_NEW_SETUP`、`EARLY_EXIT`、`STOP_WIDENED`、`STOP_DELAYED`、`MISSING_EVIDENCE`，含 `plan_adherence` 与 `strategy_alignment`；验证：规格里每个场景各一个测试，含"计划合规但策略偏离"和"止损单跳空成交不算延迟"
- [x] 4.2 实现交易员纠正的保存；验证：纠正关联到事件，被纠正的记录逐字段不变
- [x] 4.3 实现每日 KPI 并写入 `sessions`：`plan_coverage`、样本不足标注、提醒的次数与处理时长与未解除比例、纪律事件次数、可判定与未知分列；验证：4 笔开仓 3 笔有计划得 0.75 并标注、没有开仓为空、可判定 3 未知 2

## 5. watch

- [x] 5.1 定义只读适配器接口和账户快照类型，实现本地快照文件适配器；验证：读取合法文件、文件缺失和格式错误时抛读取失败；静态断言 `manual/` 不含下单、改单、撤单的调用
- [x] 5.2 实现一轮采集：取快照、写 `broker_snapshots`、更新心跳、过期判定（阈值随间隔变化）；验证：读取失败和过期时只有数据过期告警；间隔 300 秒时 400 秒前的快照不过期
- [x] 5.3 实现 `NAKED_POSITION`（宽限时间）和 `STOP_MOVED_AWAY`；验证：超过宽限触发、宽限内补上不触发、止损下移触发、上移不触发、止损消失触发
- [x] 5.4 实现 `UNPLANNED_ENTRY` 和 `REENTRY_IN_COOLDOWN`；验证：有计划不触发、没有计划触发、止损后 5 分钟同向开仓触发、冷却之后不触发
- [x] 5.5 实现 `COUNTERTREND_ADD` 和 `DAILY_LOSS_NEAR_LIMIT`；验证：熊 Spike 中加多触发、没有策略状态不触发、达到 80% 触发、上限未配置不评估
- [x] 5.6 实现提醒的记录、静默窗口去重和条件解除；验证：持续裸仓只有一条、补上止损后被标为已解除
- [x] 5.7 实现轮询间隔的配置、命令行覆盖和循环；验证：默认 60、`--interval 300` 生效、宽限和过期阈值随之变化、非法间隔被拒

## 6. scan

- [x] 6.1 实现盘中扫描：数据校验后调用引擎一步，常驻纸面运行按 scope 建立；数据异常时不产生新信号；验证：合成数据上出现 `SETUP_VALID` 加 `QUEUE_ENTRY`、缺 bar 时 `DATA_ERROR` 且无订单、重复扫描不重复
- [x] 6.2 实现历史扫描：一次性运行回放当日到决策时点；真实数据未设截止日时只返回数据校验并注明原因；验证：合成数据含决策元数据、真实数据的两种情形
- [x] 6.3 实现中文短报和资讯窗口；验证：首行与五项结构、末行免责声明、窗口为空写"不可用"、落在窗口内时列出
- [x] 6.4 展示样例：各出一条 `WAIT`、一条 `BLOCKED`、一条纸面入场、一条 `DEVIATES` 预检、一条 `NAKED_POSITION` 提醒，写进 `docs/scan-plan-watch.md`；验证：文档里的五段输出由测试生成并与实际输出一致

## 7. CLI

- [x] 7.1 `scan` 改接新的扫描函数；验证：JSON 与文本输出、退出码
- [x] 7.2 `plan`：快捷语法、`--activate`、`--cancel`、`--list`、`--json`；验证：对应的命令行测试
- [x] 7.3 `watch`：`--once`、`--interval`、`--account`、`--json`；验证：单次评估后退出，输出本轮的提醒
- [x] 7.4 `ingest --trades FILE [--order-events FILE]`；验证：输出导入、跳过、隔离的条数，行情数据不变
- [x] 7.5 更新未实现子命令的清单和测试；验证：`plan`、`watch` 不再返回 `NOT_IMPLEMENTED`，其余 9 个仍然返回

## 8. 文档与收尾

- [x] 8.1 写 `docs/scan-plan-watch.md`：三个命令的用法、预检的合成规则、成交 CSV 和订单事件 CSV 的列、快照文件的格式、六条提醒规则、纪律事件的判定表、KPI 的口径、假设；验证：design.md 的每条决策都有对应小节
- [x] 8.2 更新 `docs/glossary.md`、`README.md`；验证：新术语有条目，阶段表更新
- [x] 8.3 运行 `uv run pytest` 和 `uv run pytest -m realdata`；验证：全部通过
- [x] 8.4 按"阶段完成判定"模板写 `docs/phase-4-report.md`，在分支上提交并推送，不合并；验证：`git log phase-3-engine..phase-4-scan-plan-watch` 有提交
