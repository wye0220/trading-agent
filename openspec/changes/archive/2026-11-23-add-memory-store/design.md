## Context

`memory_items(item_id, version, …)` 和 `memory_candidates` 两张表已经存在。6A 从备注 `偏好: 键=值` 生成 `user_preference` 候选，状态 `PENDING_CONFIRMATION`。版本元组里有 `memory_version` 字段，目前一直是空。`engine/` 在基线摘要里，不能动。

## Goals / Non-Goals

**Goals:** 结构化条目与双层校验；确认、过期、否定、回滚；可按版本重现的加载；任务相关的注入与引用核对。

**Non-Goals:** 解释／纪律类评测（阶段 8）；把 `memory_version` 写进发布清单（阶段 9）；控制通道的身份校验（阶段 10）；调用模型。

## Decisions

1. **没有自由文本入口。** 每个字符串字段要么是枚举，要么是固定文法：`subject` 是登记的主体（`user_preference`、`trader`、`instrument.<根代码小写>`）；`condition` 是用 `.` 连接的 `键<比较符>值`，键和值都来自登记表；`metric`、`unit`、`source` 是枚举；`period` 是 `YYYY-MM-DD..YYYY-MM-DD`；证据 id 是标识符格式。先过 JSON schema（类型、长度、字符集），再过登记表（语义）。
2. **禁止项写在代码里，不在配置里。** 风险上限（`budget`、`limit`、`max_contracts`、`max_stops` 等）和实时状态（`position`、`pnl`、`open_risk`、`reserved` 等）的名字在校验时先于白名单被拒，原因码 `RISK_LIMIT_FORBIDDEN`、`LIVE_STATE_FORBIDDEN`。`config/memory.yaml` 里登记这类名字时配置加载失败，所以白名单扩不到它们。
3. **条目标识 = kind、scope、subject、condition、metric 的摘要。** 同一件事的新值是同一个 `item_id` 的下一个 `version`，旧版本标 `SUPERSEDED`。
4. **`memory_version` 是内容寻址的快照。** 成员是 `(item_id, version)` 的有序列表，`memory_version = mem_<成员列表的 sha256 前 16 位>`。快照不可变。回滚就是把头指针指回旧快照，相同成员集自然得到相同标识。
5. **一个全局头指针。** 加载时按条目的 scope 过滤（`global` 加当前四维 scope），并返回 `scope_digest`（实际加载内容的摘要）。每个 scope 钉住哪个 `memory_version` 由阶段 9 的发布清单决定。备选是每个 scope 一个头指针，但 `global` 条目要同步到所有 scope，复杂而没有收益。
6. **过期是加载时的过滤，不改快照。** `valid_until` 已过的条目不加载；`sweep` 只把它们的状态标成 `EXPIRED` 供展示。这样 `memory_version` 不会在没有人操作时变化。
7. **生效路径按 kind 分开。** `user_preference` 经确认直接生效。`market_fact`、`discipline_pattern` 确认后停在 `PENDING_EVALUATION`；`activate_evaluated` 要求 `evaluation_runs` 里有该候选 `status='PASSED'` 的记录，阶段 7 没有任何入口能写出这条记录。
8. **证据门槛。** `discipline_pattern` 要有 `sample_n ≥ 20`、非空的 `period` 和证据 id；`market_fact` 必须有 `valid_until`。未达到的在提出时就被拒。
9. **加载时再校验一次。** 库里的条目若不再满足 schema 或登记表（比如登记表收紧了），不加载并计入 `excluded.INVALID`。
10. **模板在代码里，标签在登记表里。** 句子只由字段和登记表里的标签拼出，末尾带 `[item_id]`。
11. **Context 构建器**按 `config/memory.yaml` 的 `relevance` 筛选：每类任务列出相关的 kind（偏好再细到键），最多 12 条。输出里引用的 `item_id` 必须是本次注入的，否则 `UNKNOWN_MEMORY_CITATION`。
12. **不改 `memory_candidates` 的列**（6A 用位置插入）。候选与条目的对应关系记在 `memory_events`。

## Risks / Trade-offs

- 文法很窄，表达不了的事实进不了 Memory → 这是有意的；要加表达力就在登记表里加键，走代码评审。
- 引擎运行的版本元组暂时不带 `memory_version` → Memory 不影响 Python 的任何决策，只影响解释；阶段 9 接上。
- 本机 CLI 当作确认入口 → 阶段 10 之前没有别的可信通道；能在本机运行命令的人本来就能改配置。

## Migration Plan

`0007_memory` 只新增表和索引。回退：删掉三张新表即可，`memory_items` 不受影响。
