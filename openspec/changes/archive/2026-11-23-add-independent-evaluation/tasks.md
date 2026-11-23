## 1. 基础
- [x] 1.1 `config/evaluation.yaml`、`evaluation/config.py`
- [x] 1.2 migration `0008_evaluation`
- [x] 1.3 `evaluation/stats.py`：配对日差、区块 bootstrap、回撤、差异交易
- [x] 1.4 `evaluation/gates.py`：盲筛、Track A／B、标注类

## 2. 流程
- [x] 2.1 `evaluation/windows.py`：分配、解封账本、退回
- [x] 2.2 `evaluation/guard.py`：身份、摘要清单、隔离检查
- [x] 2.3 `evaluation/run.py`：登记、评测库、盲筛、确认、报告

## 3. 事故回归库
- [x] 3.1 `incidents/library.py`：收录、重放、全量回归
- [x] 3.2 合成回归

## 4. 接入与验收
- [x] 4.1 CLI `evaluate`
- [x] 4.2 `tests/test_evaluation.py`、`tests/test_incidents.py`（含第 8 节的门槛自测）
- [x] 4.3 `docs/evaluation-protocol.md`、术语表、README、阶段汇报
- [x] 4.4 全量 pytest、真实库上的回归与隔离检查、核对基线摘要
