# evaluation-windows Specification

## Purpose

用只追加的解封账本（window_ledger）管理每个品种截止日之后胶囊的分配、解封和退回：每次登记冻结一个盲筛胶囊和一段确认窗，解封过的胶囊不再分配给任何新登记，每个评测阶段只能运行一次，没用到的确认窗可以退回。

## Requirements

### Requirement: 窗口分配与解封账本
窗口 SHALL 取自基线登记的封存胶囊，按日期顺序分配盲筛窗和确认窗。分配、解封、退回 SHALL 记入只追加的账本，按品种和实际日期记录。每段 SHALL 只按登记的用途解封一次。

#### Scenario: 已披露的窗口不得复用
- **WHEN** 新登记（包括改动后重新提出的候选）请求窗口
- **THEN** 任何被解封过的胶囊都不会分配给它

#### Scenario: 胶囊不够
- **WHEN** 可用胶囊少于盲筛加确认所需
- **THEN** 登记被拒，原因 `NO_WINDOW`

### Requirement: 未用的确认窗可以退回
候选止步于盲筛、确认窗从未解封、且没有别的登记在用时，确认窗 SHALL 退回可用池。

#### Scenario: 盲筛被淘汰
- **WHEN** 盲筛结果是 `SCREEN_REJECTED`
- **THEN** 它的确认窗记为 `RETURNED`，之后的登记可以分配到它

### Requirement: 共用确认窗须同时登记
多个候选共用确认窗 SHALL 只能在同一次登记里完成。

#### Scenario: 事后加入
- **WHEN** 一个窗口已分配给之前的登记
- **THEN** 新登记不会分配到它
