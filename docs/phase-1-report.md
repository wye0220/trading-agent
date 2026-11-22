# 阶段 1 汇报：数据合同与项目骨架

日期：2026-11-22。分支：`phase-1-data-contract`（未合并到 `main`）。变更：`openspec/changes/bootstrap-data-contract/`。

## 1. 完成项与未完成项

**完成**

- Prompt 0 写入 `CLAUDE.md`；术语表、数据合同文档；项目骨架、配置、`.claude/settings.json`。
- `data/GLBX-20260905-AKYFXCHGY4/` 移到 `~/trading-agent-data/databento/`，移动后 DBN 的 SHA-256 与 `manifest.json` 一致。
- 运行库（在数据根目录）与发布库（`releases/`），编号 migration，WAL，乐观锁更新。
- Databento DBN 与 CSV 导入：实际合约规范化、价差丢弃、revision、可用时点、增量、每日单一来源、可选截止日。
- 派生：5 分钟线、已完结 1 小时线、RTH 日高低差、参考价位，按决策时点查询。
- 校验、交易日状态、快照、scope 数据阻止状态。
- CLI 14 个子命令：`ingest`、`collect`、`scan` 可用，其余返回 `NOT_IMPLEMENTED`。
- 数据可得性报告。
- Webull 采集的全部逻辑（只存已收线、只追加、补缺、修订、实际合约、失败不造数、只读与凭证断言）和 launchd 样例（未安装）。

**未完成**

| 任务 | 原因 |
| --- | --- |
| 7.1 实测 Webull 能否取 ES 期货 K 线 | 项目里没有 `.env`，没有凭证，无法联网核实 |
| 7.2 Webull 适配器用录制的真实响应测试 | 没有真实响应可录。适配器已写好，目前只用按 SDK 文档假定的返回形状测试，没有冒充成录制样本 |
| 12.3 手工运行一次 `collect` | 同上 |
| 12.6 三组测试全部通过 | `webull_live` 这一组失败（`CREDENTIALS_MISSING`），另两组通过 |

Webull 采集因此**还不能算可用**：能否取到 ES 期货、代码格式、时间戳是起始还是结束时刻，都没有核实。

规格里"数据异常时已有纸面仓位保留并标为无法定价"这一句，阶段 1 没有纸面账本，无从验证，留给阶段 3。

## 2. 产物路径

- 代码：`src/trading_agent/`（`cli.py`、`config.py`、`sessions.py`、`contracts.py`、`bars.py`、`db.py`、`ingest.py`、`pipeline.py`、`derive.py`、`days.py`、`context.py`、`scan.py`、`collect.py`、`report.py`、`sources/`、`migrations/`）
- 配置：`config/instruments.yaml`、`paths.yaml`、`watch.yaml`、`collect.yaml`
- 文档：`CLAUDE.md`、`README.md`、`docs/glossary.md`、`docs/data-contract.md`、`data/README.md`
- 测试：`tests/`（合成样本在 `tests/fixtures/`）
- 调度样例：`ops/launchd/collect.plist.example`、`ops/launchd/run-collect.sh`
- 报告：`reports/data_availability.md`（由 `ingest` 生成，不进 Git）

## 3. 执行的命令和真实结果

| 命令 | 结果 |
| --- | --- |
| `uv run pytest` | 125 passed |
| `uv run pytest -m realdata` | 4 passed |
| `uv run pytest -m webull_live` | 1 failed：`CREDENTIALS_MISSING` |
| `uv run python -m trading_agent ingest`（空库） | 71.9 秒；读 3,945,840 条 = 入库 3,425,254 + 价差 520,581 + 异常 5；丢弃价差代码 132 个 |
| `uv run python -m trading_agent ingest`（重跑） | 6.8 秒；2 个文件跳过，新增 0 |
| `uv run python -m trading_agent scan --as-of 2026-09-03T15:00:00Z` | `WAIT`，`ESU2026`，来源 databento，17 根，重复运行快照摘要相同 |

真实数据的发现（详见 `reports/data_availability.md`）：

- ES 有 1760 个交易日（2019-09-05 至 2026-09-04），1756 个 RTH 日完整。
- 4 个有缺口的交易日全在 2020 年 3 月（9、12、16、18 日），是熔断停牌，不是数据缺失。
- `condition.json` 标了 18 个降级日，其中 11 个是交易日，另外 7 个是周末。
- 5 条 `OUTSIDE_SESSION`：都在 17:59–18:00 ET 的维护时段，没有入库。
- **换月规则 `roll-v1` 比成交量交叉日早 1–2 个交易日**：2019–2022 年初早 1 天，之后一直早 2 天。也就是规则换到新合约时，旧合约当天的成交量仍然更大。是否把规则改成到期日前第 6 个日历日（周一），需要你决定。
- 按当前口径可分 86 个 20 日胶囊、43 个 40 日确认段（未扣开发期和验证窗）。
- 2026-09-05 之后没有任何数据。

## 4. 新增假设及写入位置

全部在 `docs/data-contract.md` 的"假设"一节，共 10 条。与原计划不同的两处：

1. 日历用 `XNYS` 加固定 Globex 时段，没有用 `CMES`（它不区分 RTH）。已同步改了 `design.md` 决策 14。
2. 进行中的交易日：最后一根 K 线之后的缺失，在该来源当日收盘后还没有数据时不算缺口（`DAY_IN_PROGRESS`）。否则盘中采集的每一天都会被判成有缺口。

另外建了一个空目录 `~/trading-holdout/` 作为登记的保留集路径，里面没有放任何东西。

## 5. 下一阶段可复现的输入

- 运行库：`~/trading-agent-data/runtime/trading_agent.sqlite`，可由 `ingest` 从原始文件完整重建。
- 行情读取接口：`trading_agent.context` 的 `visible_bars`、`hourly_asof`、`rth_day_ranges_asof`、`reference_levels_asof`，都按决策时点返回当时可见的数据。
- 实际合约：`trading_agent.contracts.active_contract`。
- 时段与日历：`trading_agent.sessions`。

进入阶段 2 之前需要你处理的事：放 Webull 凭证并运行 `uv run pytest -m webull_live -s`；决定换月规则；决定是否补 2026-09-05 之后的 Databento 数据。
