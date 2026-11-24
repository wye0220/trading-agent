# 阶段 8 汇报：独立评测与事故回归库

分支 `phase-8-evaluate`，OpenSpec 变更 `add-independent-evaluation`（未归档）。没有合并到 `main`。

## 1. 完成项与未完成项

完成：

- `evaluate` 子命令：预登记、盲筛、正式确认、标注类评测、回归、事故草稿的试重放与收录、隔离检查、摘要清单。
- 预登记协议与不可修改的登记；窗口分配、解封账本、退回；每段只看一次；α 在解封确认窗之前登记、看到结果后消耗。
- 门槛：盲筛淘汰规则，Track A／B 与通用门槛，区块 bootstrap 置信界，差异交易检验，分列，标注类指标。
- 评测方数据库：评测在 `holdout_root` 下自己的库里回放两臂，运行库只收到允许的摘要。
- 事故回归库：条目自带行情，收录时独立重放，重放不出来的标 `INCOMPLETE`。
- 守卫：作者身份、摘要清单缺失或不符时拒绝；隔离检查如实报告四条路径。
- `docs/evaluation-protocol.md`。

未完成：

- **隔离验收没有通过。** 建立独立的系统身份、移动封存行情需要管理员权限，由你来做；步骤在协议文档"隔离"一节。
- **没有在真实胶囊上运行过任何评测**，也没有登记真实候选。除隔离外，阶段 5 的功效报告显示两条轨道都是 `EVIDENCE_INSUFFICIENT`，按现行协议候选进不了正式确认。
- **回归库是空的。** 12 份事故草稿都能独立重放，但收录要你确认，我没有替你确认。
- 跨品种联合检验没有实现（只有 ES 一个策略 scope）。
- 影子数据作为前向保留集的封存留给阶段 9。

## 2. 产物

| 类别 | 路径 |
| --- | --- |
| 代码 | `src/trading_agent/evaluation/`（`config.py`、`stats.py`、`gates.py`、`windows.py`、`guard.py`、`run.py`）；`src/trading_agent/incidents/library.py`；`cli.py` |
| 配置 | `config/evaluation.yaml` |
| migration | `src/trading_agent/migrations/runtime/0008_evaluation.sql` |
| 测试 | `tests/test_evaluation.py`（18 项）、`tests/test_incidents.py`（5 项）；`test_cli.py`、`test_cli_manual.py`、`test_risk.py`、`test_storage.py` 随新命令和 migration 更新 |
| 文档 | `docs/evaluation-protocol.md`、`docs/glossary.md`（独立评测一节）、`README.md` |
| 规格 | `openspec/changes/add-independent-evaluation/` |

## 3. 命令与真实结果

```text
uv run pytest                      694 passed, 7 deselected
uv run pytest -m realdata          6 passed
openspec validate --all --strict   全部通过
```

真实运行库：

```text
evaluate                     登记 0 个；胶囊 20 个，全部空闲；两个家族各余 5 次正式检验
evaluate --digests           清单：AUTHOR_IDENTITY（我以作者身份运行，被拒是预期行为）
evaluate --isolation-check   FAIL：python_listdir、python_open、shell_cat、symlink 四条都读得到；
                             封存行情不在保留集目录内
Read 工具读 ~/trading-holdout  被拒绝（.claude/settings.json 的规则）
evaluate --incident-list     回归库 0 个条目，待确认的草稿 12 份
evaluate --incident-check ALL  12 份草稿全部能独立重放（没有收录）
evaluate --regression        通过：事故库 0 个条目；合成回归 309 passed
```

12 份草稿里 9 份是数据问题（2020 年 2–3 月、6–7 月，2024-09-18），3 份是重大成交歧义。它们用 20 个交易日的预热，在临时库里重放出的检查点状态、决策序列、订单和交易与原来的连续回放逐条一致。

基线 `bl_6027564e58b04712` 的代码摘要没变。窗口账本、登记表、事故库都是空的。

运行库在测试过程中被升级到了 `0008_evaluation`（只新增表）。这次升级之前没有单独备份；阶段 7 之前的备份在会话临时目录。

第 8 节的门槛自测：

| 要求 | 测试 |
| --- | --- |
| 20 日好成绩无法晋级 | `test_good_twenty_days_cannot_promote` |
| 20 日显著劣化按预案停止 | `test_clearly_worse_twenty_days_stops`、`test_screen_rejection_returns_confirm_window_and_regression_blocks` |
| 未用确认窗可退回 | 同上（`RETURNED`，之后可以再分配） |
| 改了候选不能复用已披露数据 | `test_full_protocol_flow`（`CANDIDATE_CHANGED`、`NO_WINDOW`） |
| 低频和不可估标样本不足 | `test_low_frequency_and_unestimable_are_insufficient_not_fail` |
| 正式确认不能中途加天、重复查看、合并预筛数据 | `test_full_protocol_flow`（`PROTOCOL_CHANGED`、`ALREADY_VIEWED`、确认只含确认窗的日子） |
| 账户冷却和风控回归零容忍 | `test_track_b_gates`（`REGRESSION_FAILED`）、`test_regression_fails_when_behaviour_changes` |

流程测试用的是标成"真实"的合成行情：设截止日、登记胶囊、评测方库导入截止日之后的数据、两臂回放，走的是真实数据的同一条路径。

## 4. 新增假设

12 条，写在 `docs/evaluation-protocol.md` 的"假设"一节。最需要你看的：盲筛淘汰阈值是我写的预设；解封过的胶囊一律不再分配；功效资格不够的候选不进正式确认。

## 5. 下一阶段的输入

- `evaluation_runs` 里 `stage='CONFIRM'`、`status='PASS_FOR_SHADOW'` 的记录是进入影子的唯一凭据；现在没有。
- `evaluation.run.registration(candidate_id)` 给出协议和窗口；`evaluation.guard.digests()`、`verify()` 供发布入口复用。
- `incidents.library.run_all()` 是发布前回归的入口。
- `memory.store.head()` 给出要写进发布清单的 `memory_version`。
- 基线 `bl_6027564e58b04712`、3 个排队候选（都没有登记评测）。
