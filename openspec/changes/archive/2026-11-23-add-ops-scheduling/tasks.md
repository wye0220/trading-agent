## 1. 基础
- [x] 1.1 `config/ops.yaml`、`config/control.yaml`、`ops/config.py`
- [x] 1.2 migration `0010_ops`
- [x] 1.3 `ops/state.py`：运行锁、水位、心跳

## 2. 调度
- [x] 2.1 `ops/tick.py`：盘中节拍、去重、断流告警
- [x] 2.2 `ops/daily.py`：盘后流水与周度聚合

## 3. 告警与模型
- [x] 3.1 `ops/alerts.py`：分发、去重、静默、watch 停止
- [x] 3.2 `ops/explain.py`：claude -p、schema、死信与重放

## 4. 控制通道
- [x] 4.1 `ops/control.py`：白名单、身份、令牌、审计、context、watch 间隔
- [x] 4.2 适配器：本机命令行、Telegram
- [x] 4.3 人工计划预检读取 context；watch 循环每轮重读间隔

## 5. 命令、样例、测试与文档
- [x] 5.1 CLI：`ops`、`control`；`ops --health`
- [x] 5.2 launchd 样例与包装脚本（不安装）
- [x] 5.3 测试：单元与两期全链路演示
- [x] 5.4 `docs/ops-runbook.md`、术语表、README、阶段报告
