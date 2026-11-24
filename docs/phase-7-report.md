# 阶段 7 汇报：Memory 存储

分支 `phase-7-memory`，OpenSpec 变更 `add-memory-store`（未归档）。没有合并到 `main`。

## 1. 完成项与未完成项

完成：

- 条目结构、双层校验（JSON schema 加登记表）、禁止项（风险上限、实时状态）。
- 生命周期：提出、确认、拒绝、评测后生效、被否定、过期；事件只追加。
- `memory_version` 快照、头指针、回滚、按版本加载。
- 按 scope 加载、模板句子、按任务筛选的 Context 构建器、引用核对。
- CLI `memory`；`watch` 读已生效的轮询间隔偏好；人工计划的版本元组带 `memory_version`；`evolve` 为人工轨聚类生成候选。

未完成（都属于后面的阶段）：

- `market_fact` 和 `discipline_pattern` 没法生效：要等阶段 8 的解释／纪律类评测。入口函数已留。
- 引擎运行的版本元组里 `memory_version` 仍为空，等阶段 9 由发布清单带入。
- 确认走本机 CLI，控制通道在阶段 10。
- `report_max_lines` 确认后会进 Context，但还没有东西按它裁剪报告。
- 没有调用模型，所以"输出中引用 item_id"只有核对函数，没有真实的模型输出可核对。

## 2. 产物

| 类别 | 路径 |
| --- | --- |
| 代码 | `src/trading_agent/memory/`（`registry.py`、`items.py`、`store.py`、`context.py`）；`cli.py`；`manual/plans.py`；`evolve/run.py` |
| 配置 | `config/memory.yaml` |
| schema | `schemas/MemoryItem.schema.json`（新）、`schemas/MemoryCandidate.schema.json`（收紧） |
| migration | `src/trading_agent/migrations/runtime/0007_memory.sql` |
| 测试 | `tests/test_memory.py`（42 项）；`test_cli.py`、`test_cli_manual.py`、`test_llm.py`、`test_risk.py`、`test_storage.py` 随新命令、新 migration 和收紧的 schema 更新 |
| 文档 | `docs/memory.md`、`docs/glossary.md`（Memory 一节）、`README.md` |
| 规格 | `openspec/changes/add-memory-store/` |

## 3. 命令与真实结果

```text
uv run pytest                      672 passed, 7 deselected
uv run pytest -m realdata          6 passed
openspec validate --all --strict   全部通过
```

真实运行库（升级到 `0007_memory`，升级前的备份在本次会话的临时目录）：

```text
memory                             memory_version：无，生效 0 条，没有待处理的候选
memory --context SessionReview     注入 0 条
evolve --last 0 --account alpaca-paper
                                   结果 CANDIDATES（3 个候选均 UNCHANGED）
                                   Memory 候选：新增 0，跳过 EVIDENCE_INSUFFICIENT 2
```

Alpaca 人工轨的两个聚类（无计划开仓、止损后无新形态重试）只有 8 个 session 的暴露，不到 20 个样本的门槛，所以没有生成 Memory 候选。运行库里目前没有任何 Memory 条目或候选：6A 的偏好候选来自备注里的 `偏好: 键=值`，你还没有写过这样的备注。

基线 `bl_6027564e58b04712` 的代码摘要与现在一致，没有失效。

提示词要求的六项测试都有对应：

| 要求 | 测试 |
| --- | --- |
| 过期条目不加载 | `test_expired_items_are_not_loaded` |
| 任一字段夹带自由文本或指令被 schema 拒绝 | `test_free_text_in_any_field_is_rejected_by_schema`（16 种改法） |
| 引用未登记指标被拒 | `test_identifier_shaped_text_is_rejected_by_registry` |
| 候选试图写入风险上限被拒 | `test_risk_limits_and_live_state_cannot_enter_memory`、`test_registry_cannot_whitelist_forbidden_names` |
| 回滚后加载旧版本 | `test_new_value_supersedes_and_rollback_restores` |
| 模板生成句子与字段一致 | `test_sentence_is_generated_from_fields` |

## 4. 新增假设

10 条，写在 `docs/memory.md` 的"假设"一节。最需要你看的三条：本机 CLI 的确认视为你的确认；登记表（偏好键、指标、条件键、事件）是我写的预设；执行模式至少 20 个样本。

## 5. 下一阶段的输入

- `memory.store.activate_evaluated(candidate_id, evaluation_id)`：阶段 8 评测通过后调用，要求 `evaluation_runs` 里有 `status='PASSED'` 的记录。
- `memory.context.build(scope, task)` 和 `check_citations`：阶段 8 评测"加入后标注准确率是否不下降"时用。
- `memory.store.head()`、`members()`：阶段 9 把 `memory_version` 写进发布清单时用。
- 6B 的输入不变：基线 `bl_6027564e58b04712`、3 个排队候选、`candidates.register_formal_test`／`consume_formal_test`。
