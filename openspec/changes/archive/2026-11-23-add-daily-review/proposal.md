## Why

阶段 5 给出了基线，但只有汇总数字。要让策略和我的执行纪律一天天改进，需要把每个交易日拆开看：哪些机会被允许、哪些被拦、每笔结果该归到规则还是执行、我的计划做到了几成。阶段 6B 的跨日聚合也要以这些逐日观察为输入。

## What Changes

- 新增 session 封存：一个运行的一个交易日在截止强平之后封存，写入当日指标。只有封存的 session 才能复盘。
- 新增 `review` 子命令：为指定的已封存 session 生成日复盘，输出 `reports/sessions/<session_id>.md` 和 `.json`，JSON 必须通过 `SessionReview` schema。
- 复盘内容由 Python 确定性生成：机会与数据、策略轨、人工轨、归因、对 `trader_notes` 的回应、观察、事故草稿、Memory 候选。区分当时知道的事实、证据不足的判断、事后信息和改动假设。
- 事故草稿：`DATA_ERROR`、引擎异常和重大成交歧义生成草稿，保存事故前的检查点和之后的原始事件。草稿要等我确认才进回归库（阶段 8）。
- 只有指向数据或引擎问题的观察会直接产出 `PROPOSED` 候选；策略和纪律类只登记为观察。
- 保留数据的保护：晚于开发可见截止日的真实数据 session 不能由普通复盘处理。
- 新增 migration `0005_review.sql`。

## Capabilities

### New Capabilities
- `session-sealing`: session 的封存条件、当日指标和幂等。
- `daily-review`: 日复盘的输入范围、内容规则、归因、观察与候选、输出。
- `incident-drafts`: 事故草稿的触发条件、检查点和保存内容。

### Modified Capabilities
- `agent-cli`: `review` 成为已实现的子命令。
- `runtime-storage`: 新增复盘相关的表（migration 0005）。

## Impact

- 代码：新增 `src/trading_agent/review/`；改 `cli.py`。不改引擎、策略和阶段 4 的扫描器。
- 模型调用：本阶段不调用模型。复盘正文由规则生成；另外把一份压缩上下文放进已有的解释队列，供以后接入模型做叙述，不影响确定性的结论。
- 数据：`sessions`、`candidates`、`candidate_ledger` 开始有内容；新增 `session_reviews`、`observations`、`incident_drafts`、`memory_candidates`。
- 属于二期（阶段 6A），不涉及候选升级（6B）、Memory 存储（7）和评测（8）。
