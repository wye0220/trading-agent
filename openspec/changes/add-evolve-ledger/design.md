## Context

6A 的 `observations` 表一个 session 一个机制一条，`detail_json.items` 里带原因（如 `TAIL_WINDOW_UNCONFIGURED`）。`candidates` 和 `candidate_ledger` 两张表在阶段 1 就建了，6A 已往里写过 `data_engine` 类的修复请求。阶段 5 有变体工厂、`research_runs` 和逐日序列，可以直接复用来估计效应。基线代码摘要覆盖 `strategies/` 和 `engine/`，这两个目录不能动。

## Goals / Non-Goals

**Goals:** 确定性、可重复的聚合与候选生成；所有可调的数（窗口、阈值、权重、模板）都在配置里并进入输入摘要；候选只能通过白名单差异表达。

**Non-Goals:** 不调用模型；不做正式评测（阶段 8）；不实现发布指针（阶段 9）；不让截止日之后的行情进入运行库。

## Decisions

1. **聚类键**：`scope × mechanism × reason × diff_key`。`reason` 取自观察条目，没有则为空。`diff_key` 由 `config/evolve.yaml` 的 `mechanism_map` 给出；没有映射的聚类 `diff_key` 为空，只能停在 `OBSERVATION`，并注明"没有白名单内的键"。策略家族是 scope 的第一维，天然不混簇。
2. **反例**：按天定义。每个机制登记一个"暴露条件"（如 `EXPIRED_THEN_TRIGGERED` 的暴露是当日有到期订单）；窗口内满足暴露条件却没有出现该机制的 session 是反例。没有可计算暴露条件的机制，反例记为未知，可信度按配置的折扣计。
3. **候选来源**：模板和导入提案共用 `validate()`。校验顺序：schema → 类别与轨道 → 白名单（用策略的变体工厂真的构造一次）→ `from` 等于现行配置 → 证据属于某个聚类 → 天数够不够（不够降为 `OBSERVATION`）→ FAIL 重提规则 → 每家族 3 个上限。
4. **候选标识**：`cand_` + sha256(scope | category | 差异)。重复运行不产生新候选；证据变了就刷新并记台账。
5. **`base_release`**：还没有发布时写 `baseline:<baseline_id>`。基线换了即视为基准被替换。
6. **假设家族**：策略类候选取验证轨道（每个 scope 两个家族），这是最保守的 α 记账方式。α 每次取 `alpha_family / max_formal_tests`（沿用 `config/research.yaml`）。6B 只提供登记和消耗的函数与统计，调用方是阶段 8。
7. **效应估计**：用 `baseline._run` 以角色 `CANDIDATE` 在开发段加向前验证段回放候选配置，账本保留；与基线主运行逐日配对。Track B 的效应项是 `max(0, 平均日差)`，Track A 是 `max(0, 平均日差 + 非劣效界)`。没估计过的候选分数为空，排在最后。
8. **排序分**：`effect × min(1, 证据天数 / days_full_weight) × credibility`；`credibility = (1 − 反例占比，未知时取 unknown_counter_factor) × (集中在单一时段时乘 single_bucket_factor)`。
9. **前向预演**：可用日期 = 运行库里晚于基线末日、且不属于任何胶囊或保留登记的有效日。现在这个集合是空的（截止日之后的 K 线不入库），命令返回 `NO_ELIGIBLE_DAYS`。已观察日期写入 `pre_eval_shadow_days`，供阶段 8 排除。
10. **存储**：`0006_evolve.sql` 新增 `evolve_runs`、`observation_clusters`、`candidate_queue`、`pre_eval_shadow_days`。候选正文仍在 `candidates.body_json`（`{candidate, meta}`），并写 `candidates/<scope>/<id>.{json,md}`。

## Risks / Trade-offs

- 模板是我写的预设，不是数据自己"发现"的 → 写进 `docs/evolve.md` 的假设节，列入待确认项；候选只是 `PROPOSED`，不经阶段 8 不会生效。
- 按天的反例定义比较粗 → 只用于可信度折扣，不用于任何门禁。
- 估计效应时看了向前验证段 → 规格本来就要求用它；每次估计记一条开发尝试。
- 夹具数据生成的候选 → 标 `fixture`，不进队列。

## Open Questions

- 截止日之后每日采集的数据要不要以"只供预演"的身份入库（现在不入）。
- 人工轨的聚类目前没有模板，只停在 `OBSERVATION`；等确定 Alpaca 里的单是否程序下的之后再定。
