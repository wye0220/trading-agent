# 阶段 6A 汇报：日复盘

日期：2026-11-23。分支 `phase-6a-daily-review`，未合并到 `main`。

## 1. 完成项与未完成项

**完成**

- session 封存：策略轨（一个运行的一个交易日）和人工轨（一个账户的一个交易日）。
- `review` 子命令：单日、区间、带人工轨、只复盘人工轨。
- 复盘内容：机会与数据、策略轨、人工轨、归因、观察、事故草稿、对备注的回应、Memory 候选。四类信息分开。
- 输出通过 `SessionReview` schema；同一个 session 复盘两次结果相同。
- 数据／引擎问题直接立案并登记台账；策略和纪律类只登记观察。
- 保留数据保护：晚于开发可见截止日的真实数据 session 不能封存，也不能复盘。
- 在真实数据上：基线主运行的 1340 个交易日全部封存并复盘；Alpaca 模拟盘的 8 个交易日单独复盘了人工轨。

**未完成或受限**

- 不调用模型。复盘是规则生成的结构化结果，没有叙述性的文字；解释队列里排了 1348 份上下文，没有消费者。
- 人工轨的真实数据来自 Alpaca 模拟盘，其中的订单看起来是程序下的（订单编号前缀 `qb-`、`tjl-`），是否计入等用户确认。
- 事故草稿没有做独立重放验证（阶段 8）。
- Memory 候选只生成、不生效（阶段 7）。

## 2. 产物

| 类别 | 路径 |
| --- | --- |
| 代码 | `src/trading_agent/review/`（`seal`、`facts`、`rules`、`incidents`、`build`、`render`） |
| 改动 | `cli.py`；`research/metrics.py`（`load_trades` 可按交易日取） |
| 存储 | `migrations/runtime/0005_review.sql` |
| 文档 | `docs/daily-review.md`、`docs/glossary.md`、`README.md` |
| 测试 | `tests/test_review.py`（21 项） |
| 规格 | `openspec/changes/add-daily-review/` |
| 报告（不进版本控制） | `reports/sessions/<session_id>.md`、`.json`，共 1348 对 |
| 事故草稿（项目之外） | `~/trading-agent-data/incidents/drafts/`，12 份 |

## 3. 执行的命令和结果

```
uv run pytest                    → 608 passed, 7 deselected
uv run pytest -m realdata        → 6 passed
uv run python -m trading_agent review --from 2019-09-05 --to 2024-12-31 --seal     （约 10 秒）
uv run python -m trading_agent review --from 2026-11-05 --to 2026-11-20 --manual-only --seal
```

**策略轨**（基线主运行 `base:bl_6027564e58b04712`，1340 个 session）

- 归因：按规则亏损 255，按规则盈利 144，合计 399 笔，与基线的交易数一致。没有引擎异常。
- 观察，按出现的交易日数：

| 机制 | 天数 | 含义 |
| --- | --- | --- |
| `AMBIGUOUS_FILL` | 226 | 成交带歧义标记 |
| `EXPIRED_THEN_TRIGGERED` | 116 | 挂单到期未成交，之后当日价格越过了触发价 |
| `BLOCKED_SETUP` | 74 | 信号成立但没有挂单 |
| `FORCED_FLAT` | 47 | 持仓到截止强平 |
| `STOPPED_THEN_TARGET` | 45 | 止损出场后，当日价格到过原目标 |
| `FILTERED_SETUP` | 37 | 信号被过滤 |
| `DATA_QUALITY` | 9 | 数据不完整或来源降级 |
| `TWO_STOPS_HALT` | 8 | 两次止损后停手 |

- 事故草稿 12 份：9 份数据问题（2020 年 3 月的 4 个熔断日和 5 个来源降级日），3 份 `AMBIGUOUS_BAR`（2019-09-12、2023-08-21、2024-04-09）。
- 数据／引擎候选 9 个，都是上面 9 个数据问题日。

`EXPIRED_THEN_TRIGGERED` 和 `STOPPED_THEN_TARGET` 是事后信息，只说明发生过，不说明规则错了。它们是阶段 6B 聚合时值得先看的两类。

**人工轨**（账户 `alpaca-paper`，2026-11-09 至 11-19，8 个 session）

- 开仓 24 次，全部没有事前计划，计划覆盖率 0%，每天都标注样本不足。
- 23 次平仓全部归为规则执行偏差（开仓没有计划）。
- 纪律事件：`UNPLANNED` 24 次，`RETRY_WITHOUT_NEW_SETUP` 8 次（分布在 5 天）。
- 最大的一天是 2026-11-10：5 次开仓，已实现 −1,765.09 美元。

## 4. 新增假设

10 条，写在 `docs/daily-review.md` 的"假设"节。需要用户确认的主要是：

- 机制标签的集合。
- 人工轨单独封存和复盘（提示词包默认它与策略 session 同日）。
- 备注只核对两种格式（`偏好:` 和 `更正: plan_link=`）。
- Alpaca 账户里程序下的单是否计入人工轨。

## 5. 下一阶段可复现的输入

- `observations` 表：1348 个 session 的逐条观察，带机制、scope、交易日和证据。阶段 6B 的 Map 步骤按 scope × 机制聚合它。
- `session_reviews` 表和 `reports/sessions/*.json`。
- `candidates`、`candidate_ledger`：已有 9 条数据／引擎类记录。
- 复现：`uv run python -m trading_agent review --from 2019-09-05 --to 2024-12-31 --seal`。
