## 1. 基础
- [x] 1.1 `config/release.yaml`、`release/config.py`
- [x] 1.2 runtime migration `0009_release`、release migration `0002_release`
- [x] 1.3 `release/guard.py`：人工确认、发布守卫（摘要加入受保护清单）

## 2. 发布
- [x] 2.1 `release/manifest.py`：候选清单、首次基线清单与五项验收、预期区间与触发条件
- [x] 2.2 `release/pointer.py`：批准事务、幂等、回滚、只读解析
- [x] 2.3 `scanner.live_scan` 接发布库读快照；入场权限停用时冻结

## 3. 影子与冻结
- [x] 3.1 `release/shadow.py`：登记、步进两臂、实时前向预演、终点判定
- [x] 3.2 前向保留窗口从复盘和聚合输入中排除
- [x] 3.3 `release/freeze.py`：原因与解除政策、撤单

## 4. 监控
- [x] 4.1 `release/monitor.py`：滚动指标、触发、冻结、告警、幂等

## 5. 命令、测试与文档
- [x] 5.1 CLI：`shadow`、`release`、`rollback`、`freeze`、`monitor`
- [x] 5.2 测试：单元与提示词第 6 节的全部演练
- [x] 5.3 `docs/release-policy.md`、术语表、README
- [x] 5.4 全部测试、真实库上的只读命令、阶段报告

## 6. 实时行情放行（2026-11-23 用户决定）
- [x] 6.1 `config/live_data.yaml`、`live_window.py`：来源、首日、与已封存日期的冲突检查
- [x] 6.2 导入放行、截止日清理保留、切分登记排除放行日
- [x] 6.3 纸面运行验收只数按时处理的交易日
- [x] 6.4 测试与文档（数据合同、术语表、CLAUDE.md）
