## Why

阶段 6A 已经在产生 Memory 候选，但没有地方存已确认的长期记忆，也没有加载、过期和回滚。阶段 7 把 Memory 做成独立的版本化对象：条目只由结构化字段组成，没有自由文本入口，所以指令和授权写不进去。

## What Changes

- 新增 `memory/` 模块：条目校验（schema 加登记表）、模板生成句子、生命周期（确认、等待评测、否定、过期）、不可变的 `memory_version` 快照、回滚、按 scope 加载、按任务筛选的 Context 构建器。
- 新增 `config/memory.yaml`：登记偏好键、主体、指标、条件键、来源和各任务相关的条目种类。
- 新增 `schemas/MemoryItem.schema.json`；收紧 `schemas/MemoryCandidate.schema.json`（scope、unit、period、source、证据 id 改为枚举或固定格式）。
- 新增 migration `0007_memory`：`memory_versions`、`memory_head`、`memory_events`（后两张只追加）。`memory_items` 沿用阶段 1 的表。
- 新增 CLI 子命令 `memory`：列出、确认、拒绝、否定、回滚、历史、查看某类任务会注入的条目。
- `evolve` 为可升级的人工轨聚类生成 `discipline_pattern` 的 Memory 候选（样本不够的跳过并计数）。
- `watch` 的轮询间隔读已生效的偏好 `watch_poll_interval_seconds`（命令行参数优先）；人工计划的版本元组带上 `memory_version`。

## Capabilities

### New Capabilities
- `memory-store`: 条目结构与校验、禁止写入的内容、生命周期、版本快照与回滚。
- `memory-context`: 按 scope 和版本加载有效条目，模板生成句子，按任务筛选并核对引用。

### Modified Capabilities
- `agent-cli`: 新增 `memory` 子命令。
- `runtime-storage`: 新增 migration `0007_memory`。
- `evolve-aggregation`: 人工轨聚类生成 Memory 候选。
- `broker-watch`: 轮询间隔可来自已生效的偏好。

## Impact

- 不改 `engine/`、`strategies/` 和基线摘要覆盖的任何文件；基线不失效。引擎运行的版本元组里 `memory_version` 仍为空，等阶段 9 由发布清单带入。
- `market_fact` 和 `discipline_pattern` 的生效入口只留函数，要等阶段 8 的评测结果；阶段 7 里它们只能走到 `PENDING_EVALUATION`。
- 控制通道在阶段 10；在那之前，本机 CLI 的 `memory --confirm` 视为你的确认。
