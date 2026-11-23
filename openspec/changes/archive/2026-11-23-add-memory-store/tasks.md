## 1. 登记表与校验
- [x] 1.1 `config/memory.yaml` 与 `memory/registry.py`（含禁止项）
- [x] 1.2 `schemas/MemoryItem.schema.json`，收紧 `MemoryCandidate.schema.json`
- [x] 1.3 `memory/items.py`：校验、条目标识、模板句子

## 2. 存储与生命周期
- [x] 2.1 migration `0007_memory`
- [x] 2.2 `memory/store.py`：提出、确认、拒绝、评测后生效、否定、过期标记、快照、回滚

## 3. 加载与 Context
- [x] 3.1 `memory/context.py`：加载、偏好读取、任务筛选、引用核对
- [x] 3.2 `watch` 读偏好；人工计划的版本元组带 `memory_version`

## 4. 接入
- [x] 4.1 CLI `memory`
- [x] 4.2 `evolve` 生成人工轨 Memory 候选

## 5. 测试与文档
- [x] 5.1 `tests/test_memory.py` 和受影响的既有测试
- [x] 5.2 `docs/memory.md`、术语表、README、阶段汇报
- [x] 5.3 全量 pytest、真实库上运行、核对基线摘要
