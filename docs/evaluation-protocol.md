# 评测协议（阶段 8）

`evaluate` 对候选做独立评测：先预登记，再用 20 日盲筛淘汰，最后在独立的确认窗上做一次正式检验。事故回归库和合成回归在每次评测前先跑。

评测只解释白名单内的配置差异，不执行候选代码，不调用模型。评测只产出报告；发布由独立的发布入口处理（阶段 9）。

## 现状

评测真实窗口需要三个前提，现在都不满足，所以还没有解封过任何真实胶囊：

| 前提 | 现状 |
| --- | --- |
| 隔离验收通过 | 不通过。保留集目录和封存行情都能被作者身份读到（见"隔离"一节） |
| 你写入摘要清单 | 没有。写清单的入口拒绝作者身份 |
| 候选所在轨道的功效资格为 `ELIGIBLE` | 两条轨道都是 `EVIDENCE_INSUFFICIENT`（阶段 5 的功效报告） |

第三条意味着：即使隔离做好了，按现行协议，候选可以盲筛，但正式确认会直接给出 `EVIDENCE_INSUFFICIENT`，不解封确认窗。

## 命令

```bash
uv run python -m trading_agent evaluate                          # 登记、各阶段状态、窗口占用、余下的 α
uv run python -m trading_agent evaluate --register cand_… [cand_…]   # 预登记（同一次登记的候选共用窗口）
uv run python -m trading_agent evaluate --screen cand_…          # 阶段 A：盲筛
uv run python -m trading_agent evaluate --confirm cand_…         # 阶段 B：正式确认（只看一次）
uv run python -m trading_agent evaluate --labels <候选> labels.json  # 解释／纪律类与 Memory 类
uv run python -m trading_agent evaluate --regression             # 事故回归库加合成回归
uv run python -m trading_agent evaluate --incident-list          # 回归库条目和待确认的草稿
uv run python -m trading_agent evaluate --incident-check ALL     # 试着独立重放草稿，不收录
uv run python -m trading_agent evaluate --incident-confirm inc_… # 确认一份草稿并收进回归库
uv run python -m trading_agent evaluate --isolation-check        # 隔离验收
uv run python -m trading_agent evaluate --digests                # 评测器和引擎的代码摘要
uv run python -m trading_agent evaluate --accept-digests         # 把摘要写进受保护的清单（只有评测身份能做）
```

## 流程

```text
候选（PROPOSED，在队列里）
  → 预登记：冻结候选摘要、基线、协议、盲筛窗、确认窗
  → 阶段 A 盲筛（1 个胶囊，20 个有效日）
        SCREEN_REJECTED / EVIDENCE_INSUFFICIENT → 确认窗退回
        SCREEN_CONTINUE
  → 阶段 B 正式确认（6 个胶囊，120 个有效日；阶段 5 另立的协议）
        PASS_FOR_SHADOW / FAIL / EVIDENCE_INSUFFICIENT
```

每个阶段开始前依次检查：登记存在；候选的实质内容与登记时相同；`config/evaluation.yaml` 与登记时的协议相同；引擎和策略代码的摘要与基线冻结时相同；守卫通过；回归零失败。

## 预登记

登记在任何窗口解封之前完成，之后不可修改（数据库触发器拒绝改和删）。登记内容：

- 候选的内容摘要（scope、类别、轨道、差异、基准、主指标）。证据刷新不算改动；差异变了就是另一个候选，要重新提出、重新登记。
- 基线、切分登记、引擎代码摘要、策略配置摘要、成本（费用、滑点、研究风险单位）。
- 主指标：每个有效交易日的候选净 R 减基线净 R，零交易日计入。检验是单侧的。
- 阈值：见下面两节。来源是 `config/research.yaml` 的 `power`（阶段 5 预登记）和 `config/evaluation.yaml`。
- α：每个假设家族 0.05，最多 5 次正式检验，每次 0.01（Bonferroni）。
- 两段窗口的胶囊序号、日期和内容摘要。
- 该轨道的功效资格（取自阶段 5 的功效报告）。
- 作者可见的字段。
- 有效日规则：当日实际合约 RTH 完整且来源质量未降级。空仓无信号但数据完整的日子是有效日。不按收益删除日期。

`protocol_id` 是协议正文的摘要。

## 评测方数据库

截止日之后的 K 线不在运行库里。评测进程在 `holdout_root/evaluation/<切分登记>.sqlite` 建自己的数据库，从封存行情导入到最后一个胶囊的末日，然后在里面回放两臂：

- 基线和候选都从"窗口首日之前 20 个交易日"起回放到窗口末日，只度量窗口内的有效日。
- 导入后逐个胶囊核对内容摘要与登记一致，不一致报 `EVIDENCE_INSUFFICIENT`。
- 两臂用同样的日期、行情、成交模型和固定 1 手。

运行库里不会出现截止日之后的行情，也不会出现评测的逐笔交易。

## 阶段 A：盲筛

只用来淘汰，不做任何肯定性推断：不算置信界，不出 p 值，没有 `PASS_FOR_SHADOW`。

| 结果 | 条件 |
| --- | --- |
| `SCREEN_REJECTED` | 回归失败；候选出现基线没有的引擎异常标记；配对净 R／日均差低于 −0.15；或候选最大回撤比基线多出 3 R 以上 |
| `EVIDENCE_INSUFFICIENT` | 有效日不足，或窗口内容与登记不符 |
| `SCREEN_CONTINUE` | 其余 |

盲筛不扣 α。

## 阶段 B：正式确认

只对盲筛为 `SCREEN_CONTINUE` 的登记运行，只看一次，只用确认窗的日子（不并入盲筛）。

顺序：功效资格不是 `ELIGIBLE` → 直接 `EVIDENCE_INSUFFICIENT`，不解封、不扣 α。否则先跑回归，再在候选台账登记一次正式检验（额度用完则拒绝），然后才解封。看到结果后记为已消耗。

**置信界**：连续交易日的循环区块 bootstrap，区块 5 日，10000 次，固定种子，取单侧 α = 0.01 的分位数。α × 次数不到 20 时加倍重采样，到 80000 次仍不足，或有效日不到两个区块，报 `EVIDENCE_INSUFFICIENT`。

**通用门槛**

| 门槛 | 要求 |
| --- | --- |
| `REGRESSION_ZERO_FAILURES` | 事故回归和合成回归零失败 |
| `VALID_DAYS` | 有效日达到确认窗登记的天数 |
| `MIN_TRADES_PER_ARM` | 基线和候选各至少 20 笔成交 |
| `STRESS_NET_POSITIVE` | 每边多 1 tick 滑点后，候选净结果大于 0 |
| `STRESS_NOT_WORSE_THAN_BASE` | 同样压力下，候选不比基线差 |

**Track A（`PRUNING_DEFENSIVE`）**：置信下界大于 −0.05 R／日；最大回撤改善至少 10%。

**Track B（`ALPHA_EXPANSION`）**：平均配对提升至少 0.05 R／日；置信下界大于 0；最大回撤增量不超过 1 R。

**结论**

| 结果 | 条件 |
| --- | --- |
| `FAIL` | 回归失败，或样本足够而有门槛没过 |
| `EVIDENCE_INSUFFICIENT` | 有效日不足、任一臂成交数不足、置信界估不出来 |
| `PASS_FOR_SHADOW` | 全部门槛通过 |

只有 `PASS_FOR_SHADOW` 可以进入阶段 9 的影子。`EVIDENCE_INSUFFICIENT` 的候选留在没有发布权限的研究观察。

延长确认窗只能在查看它之前另立协议。不能根据已经看到的结果加天数或改阈值：登记之后改 `config/evaluation.yaml`，已登记的评测会拒绝运行（`PROTOCOL_CHANGED`）。

## 差异交易检验

对 Track A，完整报告单列"基线入场但候选拦截"的交易数、平均净 R 及其单侧置信上界，以及候选独有的交易数（过滤改变了后续路径）。它只作辅助，不改变结论；结论始终来自两套完整回放的交易日配对结果。

## 分列

完整报告按入场时段、合约、当日波动（RTH 振幅三分位）分列两臂的笔数和净 R。

## 窗口与解封账本

窗口取自基线登记的 20 个封存胶囊。`window_ledger` 只追加，按品种和行情的实际日期记录 `ASSIGNED`、`UNSEALED`、`RETURNED`。

- **分配**：按日期顺序取一段连续的空闲胶囊，前 1 个盲筛，紧接的 6 个确认。不够则 `NO_WINDOW`。
- **解封过的胶囊不再分配给任何新登记。** 这比提示词的要求更严：不判断"是否受其影响"，一律不复用。按实际日期判断，换个 scope 名字也认得出同一段日子。
- **退回**：候选止步于盲筛（或因功效资格没有进入确认），它的确认窗从未解封，且没有别的登记在用时，回到可用池。
- **共用**：多个候选共用确认窗只能在同一次 `--register` 里完成。
- 已解封的窗口可以由你另行登记后并入开发集；这一步没有自动化。

按现行协议一个候选用 7 个胶囊，20 个胶囊在不退回的情况下只够 2 个候选走完全程。

## 作者能看到什么

| 位置 | 内容 |
| --- | --- |
| 运行库 `evaluation_runs`、`reports/evaluations/` | 盲筛：状态、原因码、有效日数。确认：再加每项门槛是否通过 |
| `holdout_root/evaluation/reports/` | 完整报告：两臂的净 R、成交数、回撤，每项门槛的数值、阈值、分母，置信界，逐日序列，分列，差异交易，回归结果 |

## 标注类评测

解释／纪律类候选和 Memory 候选不改交易逻辑，用人工标注评测。标注文件：

```json
{"candidate_id": "memc_…", "samples": [
  {"sample_id": "s1", "expected": true, "predicted": true, "citations_total": 2, "citations_correct": 2}]}
```

| 门槛 | 预设 |
| --- | --- |
| 最低样本数 | 30 |
| 识别率（该标的标出来了多少） | ≥ 80% |
| 误报率（不该标的标了多少） | ≤ 10% |
| 证据引用正确率 | ≥ 95% |
| 回归零失败、代码摘要与基线一致 | 必须 |

结果是 `PASSED`、`FAILED` 或 `EVIDENCE_INSUFFICIENT`。`PASSED` 的评测记录是 Memory 条目生效入口要的凭据。标注文件只当数据。

## 事故回归库

只收录你确认的事故。`--incident-confirm` 把 6A 的事故草稿收进库里，条目自带：

- 草稿里的检查点（当时的策略私有状态、版本元组、水位、仓位、挂单）和从检查点到事故处理后的原始事件；
- 事故日及之前 20 个交易日的原始行情、交易日状态、参考位、数据异常（故障缺口保留）；
- 期望结果：检查点时刻的策略状态、事故日的决策序列、订单和交易。

收录时在临时库里从原始行情重新跑一遍引擎。四项都对得上才是 `CONFIRMED`，否则是 `INCOMPLETE`，不算覆盖。条目文件在 `<数据根目录>/incidents/library/`，不进仓库。

每次评测先重放全部 `CONFIRMED` 条目；任何一个对不上，评测直接失败。引擎修复或策略代码上线后期望结果会变，那时要先单独验收，冻结新代码，重建基线，再重新确认受影响的条目。

**合成回归**是一组固定的既有测试文件（`config/evaluation.yaml` 的 `synthetic_regression`）：因果性（前视）、成交模型、风控与权限未批准、引擎时钟与重试、异常行情、策略向量。评测进程在子进程里运行它们。

## 隔离

隔离靠操作系统权限，代码只负责校验和拒绝。评测真实窗口前检查：

1. 当前系统用户不在 `config/evaluation.yaml` 的 `author_users` 里，否则 `AUTHOR_IDENTITY`；
2. `holdout_root/protected/evaluator-manifest.json` 存在（否则 `MANIFEST_MISSING`），且与评测器代码、引擎代码的摘要一致（否则 `DIGEST_MISMATCH`）。

评测器摘要覆盖 `src/trading_agent/evaluation/`、`src/trading_agent/incidents/`、切分／度量／功效三个研究模块和两份协议配置。改动任何一个都要你 review 后用评测身份重新运行 `--accept-digests`。

**隔离验收**（`--isolation-check`）以作者身份尝试四条路径，都被挡住才算通过：

| 路径 | 2026-11-23 实测 |
| --- | --- |
| Claude 的 Read 工具 | 被挡住（`.claude/settings.json` 的拒绝规则） |
| shell 里的 `cat` | 读得到 |
| 作者身份的 Python `open()` | 读得到 |
| 经符号链接 | 读得到 |

另外，封存行情（截止日之后的 Databento 原始文件）现在还在数据根目录下，作者身份可读。

**要你做的设置**（需要管理员权限，我没有执行）：

1. 建一个独立的系统用户（例如 `trading-eval`）。
2. 把 `~/trading-holdout` 的属主改成它，权限 `700`。
3. 把含截止日之后数据的原始行情文件移到 `~/trading-holdout/data/`，在 `config/evaluation.yaml` 填 `isolation.sealed_data_root`。运行库只留截止日之前的数据（已经如此）。
4. 让评测身份能写运行库里的登记和摘要表（例如同组加组写权限）。
5. 用评测身份运行 `evaluate --accept-digests`，再用作者身份运行 `evaluate --isolation-check` 确认通过。

第 3 步有一个要先想清楚的问题：每日采集的新数据也在数据根目录下，它们晚于截止日，同样属于未来的保留集。

## 候选状态

评测之后候选的状态：

| 评测结果 | 候选状态 |
| --- | --- |
| `SCREEN_CONTINUE` | `SCREENED` |
| `SCREEN_REJECTED`、`FAIL` | `FAILED`（完全相同的差异不能再提出） |
| `PASS_FOR_SHADOW` | `PASS_FOR_SHADOW` |
| `EVIDENCE_INSUFFICIENT` | `RESEARCH_OBSERVATION` |

## 假设

以下是我定的保守解，需要你确认；改动应发生在任何窗口解封之前。

1. **盲筛的淘汰阈值**（均差 −0.15 R／日、回撤多 3 R）是我写的预设。
2. **解封过的胶囊一律不再分配**，不判断新候选是否受其结果影响。
3. **预热 20 个交易日**。真实数据上 12 份事故草稿用这个预热都能逐条对上原来的连续回放。
4. **功效资格取阶段 5 的报告**。两条轨道现在都不合格，所以按现行协议没有候选能进入正式确认。要改变这一点，只能在查看任何确认数据之前另立协议（更长的确认窗、不同的门槛），或者等出现效应更大的候选类型。
5. **bootstrap 参数**：区块 5 日、10000 次、种子 20261123、尾部至少 20 个样本。
6. **标注类门槛**（30 个样本、80%、10%、95%）是预设。标注由人来做，项目里没有模型调用。
7. **合成回归是一组既有测试文件**，不是另写的一套场景。
8. **事故条目的期望结果按当前策略版本记录。** 策略或引擎合法变更后要重新确认条目。
9. **跨品种联合检验没有实现**：目前只有 ES 一个策略 scope。
10. **影子数据预登记为前向保留集**的封存（收益、逐笔、文案样本）留给阶段 9。
11. **评测身份要能写运行库**里的登记、账本和摘要。它写不了发布库。
12. **`evaluation_runs.status = 'PASSED'`** 是标注类评测通过的记法，与阶段 7 的 Memory 生效入口一致。
