# 影子、发布、回滚与发布后监控（阶段 9）

阶段 8 的正式确认只给出 `PASS_FOR_SHADOW`。这一阶段把通过的候选接到盘中：先在影子里做运行验收，再生成发布清单，由你批准后切换该 scope 的 active 版本；发布后每天监控，触发条件时自动冻结并告警，回滚与否由你决定。

全部是纸面版本，不涉及实盘。预设值在 `config/release.yaml`，需要你确认。

## 命令

```bash
uv run python -m trading_agent shadow --register cand_…          # 为通过正式确认的候选登记正式影子
uv run python -m trading_agent shadow --step                     # 以此刻为决策时点推进影子两臂和实时前向预演
uv run python -m trading_agent shadow --finish cand_…            # 到预登记终点后判定（不到终点被拒绝）
uv run python -m trading_agent shadow --status

uv run python -m trading_agent release --initial-baseline        # 首次纸面基线的清单（scope 没有 active 时）
uv run python -m trading_agent release --prepare cand_…          # 影子通过的候选的清单
uv run python -m trading_agent release --approve cand_… --manifest-hash <sha256>   # 批准：在终端里手动输入摘要前 12 位
uv run python -m trading_agent release --status

uv run python -m trading_agent rollback --reason "…"             # 回滚：同样要在终端里手动确认
uv run python -m trading_agent freeze --reason "…" [--cancel-working]
uv run python -m trading_agent freeze --unfreeze --cause MANUAL|MONITOR --reason "…"
uv run python -m trading_agent freeze --status
uv run python -m trading_agent monitor [--day YYYY-MM-DD]        # 收盘后
```

`--scope` 缺省 `pa_brooks:ES:RTH:5m`。每个四维 scope 有自己的 active 指针、冻结状态和监控记录，互不影响。

## 版本状态

| 状态 | 含义 | 记在哪里 |
| --- | --- | --- |
| `PROPOSED` | 候选进了队列；或首次基线清单已准备 | 候选表／发布版本表 |
| `PRE_EVAL_SHADOW` | 可选：队列前三名挂实时行情做前向预演（只记可运行性） | `pre_eval_shadow_days` |
| `EVALUATED` | 正式确认通过，即阶段 8 的 `PASS_FOR_SHADOW` | 候选表 |
| `SHADOW` | 正式影子进行中，或已通过、等待准备清单和批准 | 候选表 |
| `APPROVED` → `ACTIVE` | 批准并切换 active 指针（同一事务） | 发布库为准，运行库镜像 |
| `REJECTED` | 正式确认失败（阶段 8 记作 `FAILED`），或影子运行检查不过 | 候选表 |
| `EVIDENCE_INSUFFICIENT` | 影子走完计划仍样本不足，或数据异常日超限 | 候选表 |
| `ROLLED_BACK` | 被回滚 | 候选表、发布版本表 |
| `SUPERSEDED` | 被后来批准的版本替换 | 候选表、发布版本表 |
| `ABANDONED` | 你放弃（目前没有命令自动设置） | — |

## 1. 影子

### 正式影子（SHADOW）

只接收状态为 `PASS_FOR_SHADOW` 的候选，而且 scope 已有 active 版本（基线臂要用它）。

**登记时冻结的计划**（摘要记入 `shadow_registrations.plan_sha256`，表不可修改）：

| 项 | 预设 |
| --- | --- |
| 起始日 | 缺省下一个交易日；必须晚于运行库里已有数据的最后一天，否则拒绝（`DISCLOSED_DATES`） |
| 有效 RTH 交易日 | max(20, ⌈15 ÷ (预期每日成交 × 0.8)⌉)，上限 60。预期每日成交取阶段 5 开发段；ES 约 0.3 笔／日，所以是 60 日 |
| 每臂最低可核算成交 | 15 笔 |
| 允许的数据异常日 | 0 |
| 窗口性质 | `OPERATIONAL_ACCEPTANCE`（运行验收）；可改为 `FORWARD_HOLDOUT`（前向保留） |
| 非劣效检验 | 关闭；打开时冻结界值 0.05 R／日、单侧 α 0.05、区块 bootstrap 参数（取 `config/evaluation.yaml`）、回撤增量上限 3 R |

按预期频次凑不够最低成交数时，计划里标 `low_frequency`，结果多半是 `EVIDENCE_INSUFFICIENT`。

**两臂**：基线臂用当前 active 版本的配置，候选臂用 active 配置加候选差异。两个 `live_shadow` 运行在每次 `shadow --step` 时以同一决策时点推进，各写各的账本，都受冻结约束（`MANUAL` 冻结整个 scope，影子也停）。

**判定**只在走完计划的有效日之后（`shadow --finish`）。之前运行会被拒绝（`NOT_AT_ENDPOINT`），没有延长窗口的入口。检查项：

| 检查 | 不过时 |
| --- | --- |
| 两臂账本一致：成交的订单有仓位，平掉的仓位有交易 | `REJECTED` |
| 日终没有留仓 | `REJECTED` |
| 没有引擎异常标记（`UNPRICEABLE` 等） | `REJECTED` |
| 冻结期间没有新挂单 | `REJECTED` |
| 数据异常日不超限 | `EVIDENCE_INSUFFICIENT` |
| 两臂成交都达到最低数 | `EVIDENCE_INSUFFICIENT` |
| 非劣效（打开时）：配对净 R／日的单侧下界 > −界值，回撤增量不超限 | `REJECTED`；估不出来时 `EVIDENCE_INSUFFICIENT` |

全部通过是 `PASSED`，候选保持 `SHADOW`，等你准备清单。收益只作描述。影子是运行验收，说明纸面运行受控，**不替代正式确认**。

**前向保留窗口**：窗口性质为 `FORWARD_HOLDOUT` 时，从起始日起的交易日在影子结束前不进日复盘（`review` 拒绝，原因 `FORWARD_HOLDOUT`）和聚合（`evolve` 计入排除）。

### 实时前向预演（PRE_EVAL_SHADOW）

`shadow --step` 同时为队列前三名（状态 `PROPOSED`）推进各自的 `pre_eval_shadow` 运行，只记可运行性、数据异常和引擎异常，不报告盈亏。看过的日期写入 `pre_eval_shadow_days`，不得再进入它们的正式确认窗。这是 6B 历史版预演的实时版。

## 2. 发布清单

`release --prepare` 生成清单，写到 `reports/releases/<release_id>.json` 和 `.md`，并在运行库登记为 `PROPOSED`。同样的状态得到同样的清单和 `manifest_hash`（规范化 JSON 的 sha256，不含时间戳）。

| 字段 | 内容 |
| --- | --- |
| `release_id` | `rel_` + scope、类型、候选或基线、上一版本、配置差异的摘要 |
| `previous_release`、`rollback_target` | 当前 active（首次基线为空） |
| `git_commit`、`git_dirty` | 准备时的提交 |
| `version_tuple` | 策略家族、策略版本、风控版本、prompt、memory、引擎版本、模型 |
| `digests` | 策略配置、风控版本、引擎代码、发布服务代码、memory 版本 |
| `config_diff` | 相对正式配置文件的全部差异（上一版本的加本候选的） |
| `evaluation`、`shadow` | 正式确认和影子结果的标识与摘要 |
| `scope` | 四维 |
| `expected`、`rollback_trigger` | 监控的预期区间和触发条件（见第 5 节） |
| `change_note`、`observable_costs` | 改动说明和可观察的代价 |

### 首次纸面基线（INITIAL_PAPER_BASELINE）

scope 没有 active 时用 `release --initial-baseline`：`previous_release=null`，绑定最新的冻结基线，标 `UNVALIDATED_BASELINE`，预期区间取阶段 5 开发段的基线主运行。清单附五项验收，任一不是 `PASS` 就不能批准（`ACCEPTANCE_NOT_MET`）：

| 验收 | 怎么做 |
| --- | --- |
| 工程 | 跑 `config/release.yaml` 里列的引擎、成交、风控、校验、特征测试 |
| 逐 bar 向量 | 跑策略向量与接口测试 |
| 账本 | 基线主运行和常驻纸面运行的账本一致性 |
| 只读权限 | 跑现有的"适配器里没有下单、改单、撤单调用"测试 |
| 纸面运行 | 常驻纸面运行 `paper:<scope>` 至少按时处理过 5 个交易日（当天至少一半的 K 线在收线后 5 分钟内处理；盘后补跑不算） |

这条路径只建档：不授予实盘权限，不替代策略确认，不能覆盖已存在的 active（`ACTIVE_EXISTS`）。

## 3. 人工批准

```bash
uv run python -m trading_agent release --approve <cand_… 或 baseline_id> --manifest-hash <sha256>
```

批准要同时满足：

1. **人工确认**：系统用户在 `config/release.yaml` 的 `approvers` 里，标准输入是交互终端，手动输入清单摘要的前 12 位。不满足就拒绝（`NO_HUMAN_APPROVAL`），指针不动。代理（包括我）通过命令行跑不过这一步。控制通道里经身份校验的批准留给阶段 10。
2. **发布守卫**：发布服务的代码（`src/trading_agent/release/`、`config/release.yaml`）的摘要与受保护摘要清单 `holdout_root/protected/evaluator-manifest.json` 的 `release_sha256` 一致（`RELEASE_GUARD`）。这份清单由评测／发布身份用 `evaluate --accept-digests` 写入，和阶段 8 是同一份。
3. **内容一致**：按当前状态重算清单，摘要要等于你给的值，也等于准备时登记的值（`MANIFEST_MISMATCH`）。候选在评测登记之后改过、影子候选臂用的配置与现在的不同，也都算不一致。
4. **依据仍有效**：候选所基于的版本仍是当前 active（`STALE_BASE`）；正式确认不超过 120 天、影子结果不超过 30 天（`EVIDENCE_EXPIRED`）；首次基线五项验收全过。
5. **发布库事务**：在发布库的一个 `BEGIN IMMEDIATE` 事务里再核一次当前 active（首次基线：scope 仍没有 active），写已批准清单、改 active 指针、追加 `APPROVED` 和 `ACTIVATED` 审计事件，一起提交。

同一清单再批准一次返回 `ALREADY_ACTIVE`，不写新事件。批准过、后来被回滚或替换的清单不能原样再批准（`ALREADY_RELEASED`）。

发布库（`releases/release-state.sqlite`）里，整个项目只有 `release/pointer.py` 写它；清单和审计事件只追加（触发器拒绝改写和删除）。运行库的 `release_versions` 是镜像，发布库提交后再更新；中断时以发布库为准，`release --status` 会修正镜像。

## 4. 切换、冻结与回滚

### 切换

盘中扫描在发布库的一个读事务里读 active 指针和已批准清单，用同一快照构造策略（正式配置加 `config_diff`）和版本元组。所以别的进程此刻批准新版本，这次扫描整体仍用旧版本，**下一次扫描起**才用新版本。

已有的订单和仓位记着生成时的版本元组，按原有效期、止损和截止规则完成；历史记录保留原 `strategy_family` 和 `strategy_version`。没有 active 指针时，扫描照旧运行，版本元组标 `UNRELEASED`。

### 冻结

冻结阻止新入场，已有仓位照常管理。引擎在挂单前、写锁之内重读冻结状态，所以扫描已经开始之后才冻结，这次信号也会被挡下（`BLOCKED`，原因 `FROZEN:…`）。`--cancel-working` 同时撤掉未成交的入场单（状态 `CANCELLED`，原因 `FROZEN`）。

| 原因 | 谁设 | 作用于 | 怎么解除 |
| --- | --- | --- | --- |
| `DATA` | 数据校验（断流、异常） | 该运行的风险域 | 数据恢复后自动解除 |
| `DAILY_LOSS` | 日损控制 | 指定风险域 | 下一个交易日自动解除 |
| `MANUAL` | 你（`freeze`） | 整个 scope，含影子臂 | 你在终端里解除 |
| `MONITOR` | 发布后监控 | 常驻纸面运行 | 你在终端里解除，或回滚 |
| `RELEASE` | 回滚首次基线 | 常驻纸面运行 | 只能由新的发布批准解除 |

各原因分开记录、分开解除：解除一个原因不影响其他原因。

### 回滚

```bash
uv run python -m trading_agent rollback --reason "…"
```

需要与批准相同的人工确认（输入当前 active 的 `release_id` 前 12 位）和发布守卫。

- **有上一版本**：在发布库的一个事务里把 active 指回清单登记的 `previous_release`，记 `ROLLED_BACK` 审计事件。被回滚的版本和候选标 `ROLLED_BACK`，目标版本重新 `ACTIVE`。
- **没有上一版本**（首次基线）：先在运行库冻结该 scope（原因 `RELEASE`），再把指针的 `entries_enabled` 置 0，记 `ENTRIES_DISABLED`。旧记录保留，已有仓位照常管理。

## 5. 发布后监控

`monitor` 在每个交易日收盘后，对有 active 指针的 scope 计算该版本激活以来的指标。只计入版本元组属于当前 active 版本的交易和信号，旧版本留下的仓位不算。

| 指标 | 触发条件（准备清单时算好，冻结进清单） |
| --- | --- |
| 滚动 20 个有效日的日均净值 | 低于 预期均值 − 3 × σ ÷ √20 |
| 激活以来的回撤 | 超过预期最大回撤 × 1.0 |
| 成交频率、信号频率 | 与预期之比超出 [1/3, 3] |
| `AMBIGUOUS_BAR` 占比 | 超过 max(3 × 预期, 5%)，至少 10 笔才判定 |

有效日不满滚动窗口时，滚动净值和频率两类不判定，只报告。

**预期区间**：首次纸面基线取阶段 5 开发段的基线主运行（ES 开发段：日均 +0.011 R、日标准差 0.63 R、最大回撤 22.3 R、0.30 笔／日、`AMBIGUOUS_BAR` 0.3%）。候选版本的净值和回撤也取开发段，成交与信号频率取影子期候选臂。

**触发时**：自动冻结该 scope 的纸面入场（原因 `MONITOR`），并经 `ops/notify-telegram.sh` 告警，写明触发条件和数值。回滚还是解冻由你决定。同一 scope、同一版本、同一交易日重复运行是空操作，不会重复告警。

## 6. 演练

全部在独立的测试数据库和发布库上（`tests/test_release.py`，合成行情）：

| 演练 | 测试 |
| --- | --- |
| 完整纸面晋级：首次基线 → 影子 → 清单 → 批准 → 下一次扫描使用新版本 | `test_full_paper_promotion` |
| 无人工批准 | `test_human_approval_checks`、`test_cli_release_commands` |
| 候选哈希被改、基线被更新 | `test_tampered_candidate_and_stale_base` |
| 批准重跑、首次基线审批、重复首发拒绝 | `test_initial_baseline_approval_and_duplicate_refused` |
| 扫描中切换版本 | `test_version_switch_during_scan` |
| 无上一版本时冻结 | `test_rollback_without_previous_freezes_scope` |
| 旧仓回滚 | `test_rollback_keeps_open_position_on_its_version` |
| 已披露日期复用拒绝 | `test_shadow_refusals` |
| 短确认样本不足、不到终点不能判定 | `test_shadow_endpoint_and_short_sample` |
| 影子未过 | `test_shadow_two_arms_same_clock_and_failure` |
| 并行 scope | `test_parallel_scopes_are_independent` |
| 冻结后的入场冲突 | `test_freeze_blocks_entry_even_after_scan_started` |
| 监控触发即冻结并告警 | `test_monitor_freezes_on_breach` |
| 发布守卫、审计只追加 | `test_release_code_guard`、`test_release_tables_are_append_only` |

## 假设

以下是我定的保守解，需要你确认。

1. **人工确认靠交互终端加手输摘要前缀**。它挡住无人值守脚本和代理的误操作，但不是安全边界：同一系统用户下的程序可以伪造终端。真正的边界是发布身份隔离，和阶段 8 的评测隔离一起由你设置。
2. **发布服务的代码由阶段 8 的同一份受保护摘要清单签收**（新增 `release_sha256`）。清单不存在时，任何批准和回滚都被拒绝；这意味着在你完成隔离设置、由评测／发布身份写入清单之前，真实 scope 不能发布。
3. **没有密码学签名**。"已签署清单"是发布库里只追加的清单记录加批准人、通道和时间。
4. **候选的版本状态沿用候选表**：`PASS_FOR_SHADOW` 即 `EVALUATED`，阶段 8 的 `FAILED` 视同 `REJECTED`。首次基线只有发布版本表里的记录。
5. **影子计划的预设**：20 至 60 个有效日、每臂 15 笔、不允许数据异常日、非劣效关闭、窗口性质为运行验收。ES 按开发段频次（0.30 笔／日）会排满 60 日，预期约 18 笔，打八折后约 14 笔，低于 15 笔，所以计划会标 `low_frequency`，结果可能是 `EVIDENCE_INSUFFICIENT`。
6. **冻结的作用域**：`MANUAL` 停整个 scope（含影子），`MONITOR` 和 `RELEASE` 只停常驻纸面运行，不挡进行中的影子。
7. **监控的预期区间**：候选版本的净值和回撤也用开发段基线，频率用影子期候选臂。影子样本太小，不够定净值区间。
8. **监控的触发阈值**（3σ、1 倍开发段最大回撤、频率 3 倍、`AMBIGUOUS_BAR` 3 倍或 5%）是预设。
9. **候选链只支持一层**：评测（阶段 8）的基线臂永远是冻结基线，所以候选必须基于首次纸面基线（`baseline:<id>`）。一旦某个候选版本成为 active，之后的候选在批准时会是 `STALE_BASE`，直到评测支持以发布版本为基线。
10. **没有 active 时，盘中扫描照旧运行**，版本元组标 `UNRELEASED`，不强制先建首次基线。
11. **实时前向预演的账本留在运行库**（运行 `pre:<候选>`），但不报告盈亏；历史版预演会清掉账本，实时版需要连续的状态，不能清。
12. **发布库与运行库之间不是一个事务**；中断时以发布库为准，`release --status` 修正运行库的镜像。
