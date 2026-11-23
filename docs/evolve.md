# 聚合演化与候选台账（阶段 6B）

`evolve` 把日复盘登记的观察跨日合起来，决定哪些够格变成可评测的候选，并维护候选台账和双轨队列。

它不调用模型，不读保留集、胶囊或留在库外的行情。同样的输入运行两次，聚类、候选和周报相同。候选只是待检验的假设：不经阶段 8 的独立评测和人工批准，不会改变任何正式配置。

## 命令

```bash
uv run python -m trading_agent evolve                         # 最近 60 个已复盘的 session（config/evolve.yaml）
uv run python -m trading_agent evolve --last 0 --account alpaca-paper   # 全部 session，带上人工轨
uv run python -m trading_agent evolve --import-proposals proposals.json # 另外导入提案
uv run python -m trading_agent evolve --estimate --allow-real-data      # 给排队候选估计效应（每个约几分钟）
uv run python -m trading_agent evolve --pre-eval-shadow                 # 前向预演
uv run python -m trading_agent evolve --ledger                          # 队列、台账统计、余下的 α
```

策略轨缺省读该 scope 已冻结基线的主运行；`--run-id` 可换成别的运行。默认每周最后一个交易日运行一次（调度在阶段 10）。

输出：周报 `reports/evolve/evolve_<scope>_<截至日>.md` 和 `.json`；候选 `candidates/<scope>/<candidate_id>.md` 和 `.json`。两处都不进版本控制，台账在运行库里。

## 流程

```text
已封存并已复盘的 session
  → 聚类（scope × 机制 × 原因 × 配置差异键）
  → 升级规则（至少 3 个不同交易日）
  → 提案（预登记的模板，或导入的提案）→ 校验 → 候选 + 台账
  → 队列（只收策略类）→ 效应估计 → 排序 → 前向预演
```

## 输入范围

只读状态为 `SEALED` 且已有复盘的 session，取最近 N 个。以下 session 被排除并计数：

| 排除原因 | 条件 |
| --- | --- |
| `AFTER_DEV_CUTOFF` | 策略轨、真实数据、交易日晚于开发可见截止日（或截止日没设） |
| `SHADOW_WINDOW` | 运行模式是 `pre_eval_shadow` 或 `live_shadow` |

人工轨的 session 只含你自己的计划、成交和提醒，不含行情，不受截止日限制。

## 聚类（Map）

聚类键是 `scope × mechanism × reason × diff_key`：

- `mechanism` 是 6A 的机制标签；`reason` 取自观察条目（如 `TAIL_WINDOW_UNCONFIGURED`），没有则为空。
- `diff_key` 由 `config/evolve.yaml` 的 `mechanism_map` 给出。没有登记的机制 `diff_key` 为空，只能停在 `OBSERVATION`，并注明"没有白名单内的配置键"。这类现象要改，得走人工代码分支。
- 策略家族是 scope 的第一维，不同家族、不同品种不会进同一个聚类。

每个聚类统计：出现的不同交易日数、次数、证据事件、反例、证据最集中的入场时段。

**反例按天定义**。每个机制登记一个暴露条件；窗口内满足暴露条件、却没有出现该机制的 session 是反例。

| 机制 | 暴露条件 |
| --- | --- |
| `EXPIRED_THEN_TRIGGERED` | 当日有到期订单 |
| `STOPPED_THEN_TARGET` | 当日有止损出场 |
| `FORCED_FLAT` | 当日有交易 |
| `TWO_STOPS_HALT` | 当日至少两次止损 |
| `DISCIPLINE_*` | 当日有开仓 |
| 其余 | 没有暴露条件，反例为"未知" |

每个聚类留最多 3 条正例和 3 条反例（按日期取首、中、尾）。

## 升级规则

出现在至少 `min_days`（3）个不同交易日的聚类是 `PROMOTABLE`，否则是 `OBSERVATION`。只有 `PROMOTABLE` 的聚类能支撑 `PROPOSED` 候选。单日现象或单一亏损日不会变成候选。

## 归纳（Reduce）

提示词包里这一步由模型完成。项目目前没有模型调用通道，所以做到两头：

- **出**：每个可升级聚类的归纳输入（统计摘要加最多 3 正 3 反样例，不带原始 session 文本）放进解释队列，类型 `Candidate`，超过 token 预算的不入队。没有消费者。
- **入**：`--import-proposals` 读一个提案文件，逐条走和模板相同的校验。文件内容只当数据。

```json
{"proposals": [{"cluster_id": "cl_…", "candidate": { …Candidate schema… },
                "prior_failure": {"candidate_id": "cand_…", "report": "…", "difference": "…"}}]}
```

在接入模型之前，候选来自 `config/evolve.yaml` 里**预登记的模板**：某个聚类达到升级门槛时，按模板生成候选，`from` 取现行配置。

## 候选

每个候选只改一个键，通过 `schemas/Candidate.schema.json`。校验顺序和拒绝原因：

| 原因代码 | 含义 |
| --- | --- |
| `SCHEMA_INVALID` | 没通过 schema（含一次改多个键） |
| `EVIDENCE_NOT_IN_CLUSTER` | 没对应到本次的聚类，或引用的证据不属于那个聚类 |
| `SCOPE_MISMATCH` | 候选和聚类的 scope 不同 |
| `TRACK_INVALID`、`TARGET_INVALID` | 类别与验证轨道、改动对象不匹配 |
| `NOT_WHITELISTED` | 键不在策略登记的可改键白名单（或规则开关白名单）内 |
| `FROM_MISMATCH` | `from` 不等于现行配置 |
| `NO_CHANGE`、`INVALID_VARIANT` | 没改任何值；或改完构造不出合法的策略配置 |
| `CATEGORY_NOT_SUPPORTED` | `data_engine` 类问题由日复盘立案，不在这里提出 |
| `ALREADY_FAILED` | 完全相同的差异已经评测失败过 |
| `PRIOR_FAILURE_NOT_REFERENCED` | 同机制的候选失败过，重提没有引用失败报告并说明差异 |
| `LIMIT_REACHED` | 每个策略家族每次运行最多新生成 3 个 |

证据不足 3 天的提案以 `OBSERVATION` 状态保存，不进队列。没有任何候选时结果是 `NO_CANDIDATE`。

候选标识由 scope、类别和差异决定，所以重复运行不会产生重复候选：证据变了就刷新，否则不动。

风险上限、权限和评测协议的键不在任何白名单里，候选碰不到它们。

## 台账

`candidate_ledger` 只追加。事件：

| 事件 | 何时 |
| --- | --- |
| `CREATED` | 候选生成 |
| `EVIDENCE_REFRESHED` | 同一候选的证据更新 |
| `STATUS_CHANGED` | 状态变更，带前后状态和原因 |
| `QUEUED` | 进入队列 |
| `DEV_ATTEMPT` | 做了一次效应估计（看了开发段和向前验证段的结果） |
| `PRE_EVAL_SHADOW` | 做了一次前向预演 |
| `FORMAL_TEST_REGISTERED` | 评测角色接触最终窗口之前登记一次正式检验 |
| `FORMAL_TEST_CONSUMED` | 首次查看正式检验的结果 |

失败和放弃的候选保留。

**α 额度**按 scope 和假设家族统计。策略类候选的假设家族取验证轨道，所以每个 scope 有两个家族。每个家族 α = 0.05，最多 5 次正式检验，每次 0.01（`config/research.yaml`）。额度用完后登记被拒绝（`ALPHA_EXHAUSTED`）。登记和消耗的函数由阶段 8 调用，6B 只提供函数和统计。

## 队列

只有 `strategy_rule` 类、状态 `PROPOSED`、不是合成数据得到的候选进队列，每个都标注验证轨道：

- `PRUNING_DEFENSIVE`（Track A）：入场剪枝、时段或状态过滤。正式门槛是净 R／日非劣效加回撤改善。
- `ALPHA_EXPANSION`（Track B）：放宽条件、改核心定义或新增模式。正式门槛是净提升。

其他类别不进策略队列。

**效应估计**（`--estimate`）：在开发段加向前验证段回放候选配置，与基线主运行逐日配对，给出各段的平均日差、成交数和回撤。这是描述性的，只用来排队，不是正式检验；每次估计在台账记一条开发尝试。

**排序分**的公式和权重预登记在 `config/evolve.yaml`，运行时只填输入：

```text
分数 = 效应项 × 证据天数权重 × 机制可信度
效应项        ALPHA_EXPANSION：max(0, 平均配对日差)
              PRUNING_DEFENSIVE：max(0, 平均配对日差 + 非劣效界 0.05)
证据天数权重  min(1, 证据天数 / 20)
机制可信度    (1 − 反例占比；反例未知时取 0.5) ×（80% 以上证据集中在同一入场时段时再乘 0.5）
```

没估计过的候选分数为空，排在最后。

**过期与替换**：排队超过 120 天，或候选的基准已不是当前基准，转 `SUPERSEDED`。基准换了的可以对着新基准重新提出（同一个候选标识，状态回到 `PROPOSED`，旧的效应估计清掉）；只是过期的不会自动复活。

**基准**：还没有发布（阶段 9）时，`base_release` 写 `baseline:<baseline_id>`。基线重新冻结即视为基准被替换。

**前向预演**（`--pre-eval-shadow`）：对排名前三的候选，在"已入库、有效、晚于基线数据末日、且不属于基线登记的任何胶囊"的交易日上回放，只检查能不能跑、有没有引擎异常标记，不报告盈亏，回放的账本随即删除。看过的日期写入 `pre_eval_shadow_days`，不得再进入该候选的正式确认窗。没有符合条件的日期时返回 `NO_ELIGIBLE_DAYS`。

## 跨品种

同一策略家族、同一机制、同一原因、同一差异键的可升级聚类出现在多个品种时，标为一个候选组（`group_id`）。组内每个 scope 各自生成候选、各自评测。差异键不是品种专属（不以 `groups.` 开头）时，组可以申请 `CROSS_ASSET_POOLED` 作为补充。目前只有 ES 一个策略 scope，所以没有候选组。

## 假设

以下是我定的保守解，需要你确认；改动应发生在查看任何确认结果之前。

1. **模板是我写的预设**，不是数据"发现"的结论。现在登记了三个：尾盘窗口 30→15 分钟（Track B）、尾盘窗口 30→60 分钟（Track A）、止损偏移 1→2 tick（Track B）。前两个方向相反，由不同的现象支撑，各自检验。
2. **机制到差异键的映射**只登记了三条。`EXPIRED_THEN_TRIGGERED`（挂单有效根数）、`TWO_STOPS_HALT`（每日止损次数上限）、`BLOCKED_SETUP`（同向已有仓位）对应的键都不在策略的可改键白名单里，所以只停在观察。
3. **假设家族取验证轨道**。这是最省 α 的记法：每个 scope 的每条轨道一共 5 次正式检验。
4. **反例按天、按暴露条件定义**，比较粗，只用于排序里的可信度折扣，不用于任何门禁。
5. **排序权重**（20 天满权重、反例未知折半、单一时段折半）是预设。
6. **前向预演现在没有可用的日期**：截止日之后的 K 线不进运行库。每日采集的新数据要不要以"只供预演"的身份入库，等你定。
7. **人工轨的聚类没有模板**，只停在观察。Alpaca 里的单是否程序下的还没确认；规则开关和模板文件的白名单目前是空的。
8. **`Candidate` schema 改了一处**：`exact_diff` 的 `from`／`to` 允许标量数组，因为过滤器豁免列表是数组。
9. 策略模块用两个模块级约定登记给 6B：`EVOLVABLE_KEYS`（可改键白名单）和 `read_raw_config`（现行配置）。没有登记的策略，什么都不能改。
10. `evolve` 一次只处理一个策略运行（缺省是基线主运行）。等常驻纸面运行有了已复盘的 session，每周的输入应换成它。
