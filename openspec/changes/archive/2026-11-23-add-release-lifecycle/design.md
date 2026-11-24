## Context

阶段 8 给出 `PASS_FOR_SHADOW`；发布库 `releases/release-state.sqlite` 在阶段 1 已有 `active_pointers` 和 `release_events`，没有任何写入方。引擎在挂单前（写锁之内）已重读 `freeze_events`；`versions.current` 已能从发布库读 `release_id` 和 `manifest_hash`，但盘中扫描还没把发布库传进去。`engine/`、`strategies/` 在基线代码摘要里，不能改。

## Goals / Non-Goals

**Goals:** 影子运行验收、发布清单、人工批准、按 scope 切换、冻结与回滚、发布后监控、完整的纸面晋级演练。

**Non-Goals:** 实盘权限；控制通道（阶段 10）；操作系统层面的发布身份隔离（与阶段 8 的评测隔离一起由你设置）；定时调度（阶段 10）。

## Decisions

1. **两份状态**。发布库是 active 指针的唯一来源；运行库的 `release_versions` 是镜像和工作台（清单、状态、影子结果）。批准先写发布库，成功后再更新镜像；`release --status` 以发布库为准并修正镜像。
2. **版本状态**。候选沿用 `candidates.status`：`PROPOSED →（队列预演）→ SCREENED → PASS_FOR_SHADOW（即 EVALUATED）→ SHADOW → APPROVED → ACTIVE`，另有 `REJECTED`、`EVIDENCE_INSUFFICIENT`、`ROLLED_BACK`、`SUPERSEDED`、`ABANDONED`。阶段 8 的 `FAILED` 视同 `REJECTED`。首次基线只有发布记录：`PROPOSED → APPROVED → ACTIVE`。
3. **配置差异累积**。发布清单里的 `config_diff` 是相对正式配置文件的全部差异（上一版本的差异加本候选的差异）。盘中策略 = `registry.variant(family, config_diff)`；没有差异时用正式配置本身。
4. **读快照**。`scanner.live_scan` 在发布库上开读事务，读指针和清单、构造策略，再把同一连接交给 `runner.step` 生成版本元组，结束后提交。切换因此只在步与步之间发生。
5. **人工确认**。CLI 的批准、回滚、解除 `MANUAL`／`MONITOR` 冻结要求：系统用户在 `config/release.yaml` 的 `approvers` 里，且标准输入是终端、手动输入清单摘要（或 scope）前 12 位。这挡住无人值守脚本和代理的误操作，不是安全边界；真正的边界是发布身份隔离。
6. **发布守卫**。发布服务代码（`release/`、`config/release.yaml`）的摘要加入阶段 8 的受保护摘要清单；批准和回滚前核对，清单缺失或不符就拒绝。
7. **冻结的作用域**。`MANUAL` 冻结整个 scope（`*`，包括影子臂）；`MONITOR` 和 `RELEASE` 只冻结常驻纸面运行的风险域，不挡正在进行的影子。
8. **影子计划**。有效日数 = max(20, ⌈15 ÷ (预期每日成交 × 0.8)⌉)，上限 60；预期每日成交取阶段 5 开发段。每臂最低 15 笔。非劣效检验默认关闭。全部在登记时冻结进计划摘要。
9. **监控触发条件**在准备清单时算好冻结进清单：滚动 20 个有效日的净 R／日低于预期均值 − 3σ/√20；激活以来回撤超过开发段最大回撤；成交或信号频率与预期之比超出 [1/3, 3]；`AMBIGUOUS_BAR` 占比超过 max(3 × 预期, 5%)（至少 10 笔）。
10. **真实发布库本阶段不写**。演练用临时目录里的运行库和发布库；读路径以只读方式打开真实发布库，不迁移。

## Risks / Trade-offs

- 两库之间不是一个事务：批准在发布库提交后、镜像更新前中断，镜像会落后；以发布库为准、`release --status` 修正。
- 交互终端确认能被伪终端绕过；在发布身份隔离之前，它只防误操作。
- 真实数据上今天跑不了影子和首次基线（没有截止日之后的行情进运行库）。
