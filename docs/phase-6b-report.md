# 阶段 6B 汇报：聚合演化与候选台账

日期：2026-11-23。分支 `phase-6b-evolve`，未合并到 `main`。

## 1. 完成项与未完成项

**完成**

- `evolve` 子命令：聚合、导入提案、效应估计、前向预演、台账视图。
- Map：按 scope × 机制 × 原因 × 配置差异键的确定性聚类，带反例、样例和入场时段集中度。
- 升级规则（至少 3 个不同交易日）、每家族每次 3 个候选的上限、`NO_CANDIDATE`。
- 候选校验、落盘（表、JSON、Markdown）、只追加的台账、α 额度统计、失败后重提规则。
- 双轨队列：入队、预登记的排序分、过期与基准替换、对着新基准重新提出。
- 跨品种候选组的判定（目前只有 ES，一个组也没有）。
- 在真实运行库上跑过：聚合、三个候选的效应估计、前向预演。

**未完成或受限**

- Reduce 不调用模型。归纳输入只入队（11 份，没有消费者）；候选来自预登记的模板或导入文件。
- 前向预演返回 `NO_ELIGIBLE_DAYS`：截止日之后的 K 线不进运行库，没有可用的日期。
- 人工轨的两个聚类没有模板，只停在观察。
- 正式检验的登记与消耗只有函数，没有调用方（阶段 8）。
- 每周定时运行未接（阶段 10）。

## 2. 产物

| 类别 | 路径 |
| --- | --- |
| 代码 | `src/trading_agent/evolve/`（`config`、`aggregate`、`candidates`、`queue`、`run`） |
| 改动 | `cli.py`；`schemas/Candidate.schema.json`（`from`／`to` 允许标量数组） |
| 配置 | `config/evolve.yaml` |
| 存储 | `migrations/runtime/0006_evolve.sql`（`evolve_runs`、`observation_clusters`、`candidate_queue`、`pre_eval_shadow_days`） |
| 文档 | `docs/evolve.md`、`docs/glossary.md`、`README.md` |
| 规格 | `openspec/changes/add-evolve-ledger/` |
| 测试 | `tests/test_evolve.py`（23 项） |
| 生成物（不进版本控制） | `reports/evolve/`、`candidates/pa_brooks_ES_RTH_5m/` |

没有改 `strategies/`、`engine/` 等基线代码摘要覆盖的文件，基线 `bl_6027564e58b04712` 的代码摘要核对一致。

## 3. 命令与真实结果

```text
uv run pytest -q                                   → 630 passed, 7 deselected
uv run pytest -q -m realdata                       → 6 passed
uv run python -m trading_agent evolve --last 0 --account alpaca-paper
uv run python -m trading_agent evolve --estimate --allow-real-data
uv run python -m trading_agent evolve --pre-eval-shadow       → NO_ELIGIBLE_DAYS
```

**聚合**（1340 个策略 session，2019-09-05 至 2024-12-31；8 个人工轨 session）：11 个聚类，全部达到 3 天。

| 机制 | 天数 | 反例天数 | 配置差异键 |
| --- | --- | --- | --- |
| `AMBIGUOUS_FILL` | 226 | 未知 | 无 |
| `EXPIRED_THEN_TRIGGERED` | 116 | 22 | 无（挂单有效根数不在白名单） |
| `BLOCKED_SETUP`（同向已有仓位） | 74 | 未知 | 无 |
| `FORCED_FLAT` | 47 | 270 | `params.discipline.tail_window_minutes` |
| `STOPPED_THEN_TARGET` | 45 | 163 | `params.entry.stop_offset_ticks` |
| `FILTERED_SETUP`（尾盘窗口） | 37 | 未知 | `params.discipline.tail_window_minutes` |
| `TWO_STOPS_HALT` | 8 | 34 | 无 |
| `DATA_QUALITY`（两个原因） | 5、4 | 未知 | 无 |
| 人工轨 `DISCIPLINE_UNPLANNED` | 8 | 0 | 无 |
| 人工轨 `DISCIPLINE_RETRY_WITHOUT_NEW_SETUP` | 5 | 3 | 无 |

**候选与效应估计**（1311 个度量日，基线 392 笔、0.0259 R／日）：

| 候选 | 轨道 | 改动 | 成交数 | 平均配对日差（R／日） | 开发段 | 向前验证段 | 最大回撤（R） | 排序分 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cand_c134cf6189762cfd` | Track A | 尾盘窗口 30→60 分钟 | 363 | −0.0096 | −0.0077 | −0.0177 | 22.28→21.07 | 0.0060 |
| `cand_80c16873c935bedb` | Track B | 尾盘窗口 30→15 分钟 | 410 | +0.0005 | −0.0000 | +0.0028 | 22.28→22.59 | 0.0001 |
| `cand_979dda8f6cd1e079` | Track B | 止损偏移 1→2 tick | 388 | −0.0013 | −0.0011 | −0.0025 | 22.28→21.59 | 0.0000 |

三个候选的效应都接近零。阶段 5 的功效测算给出的可检出效应约为 0.08–0.13 R／日，这三个估计比它小一到两个数量级：

- 尾盘窗口加到 60 分钟：少做 29 笔，净 R／日下降，回撤只改善 5%，达不到 Track A 要求的 10%。
- 尾盘窗口减到 15 分钟：多做 18 笔，净结果几乎不变。
- 止损放远 1 tick：结果略差。"止损后价格又到目标"出现了 45 天，但放远 1 tick 救不回来。

按现行协议只够评测 2 个候选。就这些估计看，没有一个值得花胶囊；这是描述性结论，不是正式检验。台账记了 3 次开发尝试（Track A 1 次，Track B 2 次），正式检验 0 次，α 未动用。

## 4. 新增假设

写在 `docs/evolve.md` 的"假设"一节，共 10 条。最需要确认的：

1. 三个模板和三条机制映射是我写的预设。
2. 假设家族取验证轨道（每个 scope 每条轨道共 5 次正式检验）。
3. 排序权重（20 天满权重、反例未知折半、单一时段折半）。
4. 截止日之后的每日采集数据不入运行库，所以前向预演现在没有可用日期。
5. `Candidate` schema 允许数组值的差异。

## 5. 下一阶段可复现的输入

- 候选：`candidates` 表（3 个 `strategy_rule`，状态 `PROPOSED`；另有 6A 的 9 个 `data_engine`）。
- 队列：`candidate_queue`，含效应估计和排序分。
- 台账：`candidate_ledger`；`candidates.register_formal_test`／`consume_formal_test` 供阶段 8 调用。
- 候选回放：`research_runs` 里角色 `CANDIDATE` 的三次运行，账本保留。
- 基线 `bl_6027564e58b04712`，评测协议标识见各候选的 `evaluation_protocol_id`。
