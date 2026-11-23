# 阶段 5 汇报：回放与基线

日期：2026-11-22。分支 `phase-5-replay-baseline`，未合并到 `main`。

## 1. 完成项与未完成项

**完成**

- `replay` 子命令：在一段日期上回放，复用阶段 3 的引擎和 `StrategyProvider`。
- `baseline` 子命令：登记切分 → 冻结清单 → 回放两次比对 → 基线报告 → 预登记扰动 → 功效报告。
- 开发可见截止日的执行：2024-12-31 之后的 802,180 条 1 分钟线已移出运行库，只留覆盖和内容摘要。
- 数据切分登记：开发段、向前验证段、20 个封存的胶囊。
- 策略配置变体：按白名单内的键路径差异构造变体，供扰动和以后的自动演化使用。
- 在真实 ES 数据上冻结了第一份基线 `bl_e751e3a271af2c90`。
- 用户确认的值已写入配置：截止日 2024-12-31、费用 $5.00 往返、研究风险单位 $500、止损与强平滑点 1 tick。
- 测试与用户配置解耦：测试改用中性的配置副本。

**未完成或受限**

- 保留数据没有真正隔离：截止日之后的原始文件仍在数据根目录，作者身份读得到。隔离属于阶段 8。
- 风险规模结果没有数字：账户限额还没有填写和批准。
- 引擎的一个标记问题没有修（见第 4 节）。
- 20 个胶囊里只给前 3 个标了默认用途，解封账本属于阶段 8。

## 2. 产物

| 类别 | 路径 |
| --- | --- |
| 代码 | `src/trading_agent/research/`（`config`、`splits`、`baseline`、`metrics`、`power`、`report`、`run`）、`contenthash.py`、`strategies/variants.py` |
| 改动 | `ingest.py`、`pipeline.py`、`cli.py`、`strategies/pa_brooks.py`、`strategies/registry.py` |
| 存储 | `migrations/runtime/0004_research.sql` |
| 配置 | `config/research.yaml`；`config/paths.yaml`、`config/risk.yaml` 写入确认值 |
| 文档 | `docs/replay-baseline.md`、`docs/glossary.md`、`README.md`、`CLAUDE.md` |
| 测试 | `tests/test_research.py`、`tests/test_cli_research.py`、`tests/test_realdata.py`（按截止日改写） |
| 规格 | `openspec/changes/add-replay-baseline/` |
| 报告（不进版本控制） | `reports/baseline_pa_brooks_ES_RTH_5m.md`、`.json`、`baseline_trades_….csv`、`baseline_power_….md`、`.json` |

## 3. 执行的命令和结果

```
uv run pytest                                  → 566 passed, 7 deselected
uv run pytest -m realdata                      → 6 passed, 567 deselected in 16.94s
uv run python -m trading_agent ingest          → 从运行库移出 802,180 条，8.6 秒
uv run python -m trading_agent baseline --allow-real-data --jobs 4
```

**基线**（`pa_brooks:ES:RTH:5m`，固定 1 手，2019-09-05 至 2024-12-31）

- 状态 `FROZEN`。两次回放各约 11 分钟，399 笔交易、569 张订单、104,117 条决策记录的摘要全部相同。
- 计入度量的有效日 1311 个（开发段 1060，向前验证段 251），预热 20 日。

| 指标 | 开发段 | 向前验证段 | 合计 |
| --- | --- | --- | --- |
| 机会数 | 521 | 130 | 651 |
| 成交数 | 318 | 74 | 392 |
| 无交易日占比 | 76.1% | 77.3% | 76.4% |
| 净盈亏（美元） | 5,985.00 | 10,967.50 | 16,952.50 |
| 净 R／日 | 0.0113 | 0.0874 | 0.0259 |
| 胜率 | 34.0% | 43.2% | 35.7% |
| PF | 1.08 | 1.75 | 1.19 |
| 最大回撤（美元） | 11,140.00 | 2,725.00 | 11,140.00 |
| 每边多 1 tick 滑点后的净盈亏 | −1,965.00 | 9,117.50 | 7,152.50 |

- 开发段接近打平，多 1 tick 滑点即转亏。利润集中在 2024 年。
- 做空净 +$20,830（209 笔），做多净 −$3,877.50（183 笔）。
- 66.3% 的成交带 `AMBIGUOUS_ENTRY`：入场那根子 K 跨着挂单激活时刻。
- 没有跳空越过止损的交易；全部交易日都有 1 分钟子 K。
- 163 张订单到期未成交。

**功效测算**

- 开发段日均成交 0.30 笔，40 日窗口期望 12 笔，低于最低成交数 20 笔。
- 两条轨道的短窗正式资格都是 `EVIDENCE_INSUFFICIENT`：整体通过率 Track A 0.0%，Track B 0.1%。卡住的是最低成交数这一条。
- 配对日差标准差 0.22–0.52 R。独立正态近似下检验 δ=0.05 R／日约需 1084 个确认日，可用 400 个；当前数据可检验的最小 δ 约 0.082 R／日。

**数据切分**（`split_bb64cfdf5f0244a0`）

- 开发段 2019-09-05 至 2023-12-29，1080 个有效日。向前验证段 2024 全年，251 个。
- 胶囊 20 个，400 个有效日，2025-01-02 起，全部 `SEALED`。余量 14 日。按默认流程可评测 6 个候选。

## 4. 新增假设与发现

假设共 13 条，写在 `docs/replay-baseline.md` 的"假设"节。需要用户确认的主要是：

- 开发段与向前验证段的分界 2024-01-01。
- 门槛预设：家族 α 0.05、5 次正式检验、非劣效边界和 δ 都是 0.05 R／日、最低成交数 20 笔。
- 四个扰动的选择。

发现的问题，写在同一文档的"已知问题"节：

- 截止强平的 47 笔交易带着多余的 `UNPRICEABLE`、`UNABLE_TO_FLATTEN` 标记，并产生 108 条误报告警。盈亏不受影响。属于阶段 3 的引擎，没有改。
- 并行跑扰动第一次因数据库锁失败了三个，放宽等锁时间后重跑通过。

## 5. 下一阶段可复现的输入

- 基线 `bl_e751e3a271af2c90`，主运行 `base:bl_e751e3a271af2c90`（运行库里保留全部明细）。
- 切分登记 `split_bb64cfdf5f0244a0`。
- 四个扰动的运行 `var:bl_e751e3a271af2c90:<id>`（开发段，保留交易和订单）。
- 复现：`uv run python -m trading_agent baseline --allow-real-data`。清单不变时直接复用。
- 阶段 6A（日复盘）读主运行的 `scan_events`、`paper_trades` 和 `reports/baseline_*.json`。
