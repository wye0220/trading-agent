# trading-agent

ES、RTH 5 分钟、行情只读、纸面模拟。没有任何实盘下单功能。

规格来源是 `trading_agent_v2_6_pa.md`（Claude 提示词包 v2.6）。术语见 `docs/glossary.md`。

| 阶段 | 状态 | 文档 |
| --- | --- | --- |
| 1 数据合同与项目骨架 | 完成 | `docs/data-contract.md` |
| 2 策略插件接口与 PA v1.0 | 完成（策略还没有接进 `scan`，也没有在真实数据上运行过） | `docs/strategy-spec.md` |
| 3 引擎核心 | 完成（成交模型、风控、逐步运行器；还没有命令行入口，也没有在真实数据上运行过） | `docs/engine.md` |
| 4 scan、plan、watch 与人工成交导入 | 完成（没有实时行情和真实券商接口，盘中功能如实报告没有数据） | `docs/scan-plan-watch.md` |
| 5 回放与基线 | 完成（基线在 2019-09-05 至 2024-12-31 的真实 ES 数据上冻结；之后的数据封存） | `docs/replay-baseline.md` |
| 6A 日复盘 | 完成（规则生成，不调用模型；基线主运行 1340 天和 Alpaca 人工轨已复盘） | `docs/daily-review.md` |
| 6B 聚合演化及以后 | 未开始 | — |

## 环境

需要 [`uv`](https://docs.astral.sh/uv/)。它会自己准备 Python 3.12。

```bash
uv sync
uv run pytest                 # 单元测试，全部用合成数据
```

## 数据放在哪

真实行情和运行数据库都在项目之外，默认 `~/trading-agent-data/`（`config/paths.yaml`，可用 `TRADING_AGENT_DATA_ROOT` 覆盖）：

```
~/trading-agent-data/
  databento/GLBX-20260905-AKYFXCHGY4/     Databento 历史文件
  webull/ES/<合约>/<交易日>.csv            collect 每次追加
  runtime/trading_agent.sqlite            运行库
```

新的 Databento 文件（`ohlcv-1m` 的 `.dbn` 或 `.dbn.zst`）或符合数据合同的 CSV，放进这个目录的任意子目录，再运行一次 `ingest` 即可。

## 命令

```bash
# 导入数据根目录里新增或变化的内容，并生成 reports/data_availability.md
uv run python -m trading_agent ingest
uv run python -m trading_agent ingest --json
uv run python -m trading_agent ingest --through 2024-12-31    # 晚于该交易日的 K 线不进运行库（缺省取 config/paths.yaml）；库里已有的会被移出
uv run python -m trading_agent ingest --rebuild               # 改了换月规则等配置后：从原始 K 线重算派生数据和交易日状态

# 数据校验后运行策略和纸面账本，输出中文短报
uv run python -m trading_agent scan                                         # 盘中
uv run python -m trading_agent scan --as-of 2026-09-03T15:00:00Z            # 历史。真实数据未设截止日时只做数据校验
uv run python -m trading_agent scan --scope pa_brooks:ES:RTH:5m --json

# 人工交易计划：登记并预检（只给结论，不下单）；激活才预留预算
uv run python -m trading_agent plan ES L 5000 4995 5010 1 PA Spike H1 --account U123
uv run python -m trading_agent plan --activate plan_xxxxxxxxxxxx
uv run python -m trading_agent plan --list

# 盘中只读纪律提醒：读 ~/trading-agent-data/broker/<账户>/snapshot.json
uv run python -m trading_agent watch --once --account U123
uv run python -m trading_agent watch --account U123 --interval 300

# 股票类（SPY、QQQ）走同样的命令，数量是股数；默认账户是 Alpaca 模拟盘
uv run python -m trading_agent plan SPY L 700.50 699.00 705 100 回调买入
uv run python -m trading_agent ingest --alpaca          # 只读导入 Alpaca 的订单记录和成交

# 导入人工成交和订单事件
uv run python -m trading_agent ingest --trades trades.csv --order-events orders.csv

# 回放一段日期（真实数据要显式允许，且不得晚于开发可见截止日）
uv run python -m trading_agent replay --from 2024-01-02 --to 2024-01-31 --allow-real-data

# 冻结基线：登记切分、回放两次校验一致、出基线报告和功效测算（全量约一小时）
uv run python -m trading_agent baseline --allow-real-data --jobs 4
uv run python -m trading_agent baseline --allow-real-data --skip-power      # 只出基线报告

# 日复盘：先封存再复盘，输出到 reports/sessions/
uv run python -m trading_agent review --day 2024-06-12 --seal
uv run python -m trading_agent review --day 2026-11-10 --manual-only --seal    # 只复盘人工轨

# 只读采集已收线的 5 分钟线，追加到 CSV 并入库（暂不可用，见下文）
uv run python -m trading_agent collect
```

`--scope` 的格式是 `strategy_family:instrument_root:session_type:timeframe`，缺省 `pa_brooks:ES:RTH:5m`。

其余子命令（`evolve`、`evaluate`、`shadow`、`release`、`rollback`、`freeze`、`monitor`）返回 `NOT_IMPLEMENTED`，由后续阶段实现。

## 每日采集（Webull MCP 连接器）

2026-11-23 起，ES 和 NQ 的 5 分钟线通过 Claude 里的 Webull MCP 连接器每日保存：会话里取数，`collect --from-mcp <JSON 文件>` 落盘入库。细节和限制见 `docs/data-contract.md` 的"Webull MCP 连接器"。这些数据晚于开发可见截止日，只存文件，价格不进运行库。

## 无人值守的实时采集（暂不可用）

**现状（2026-11-22）：Webull 账号没有美国期货行情，`collect` 取不到 ES 的 K 线，实时来源暂缓接入。** 阶段 2–5 只用历史数据，不受影响。等做到盘中 `scan`、`watch` 时再定来源。

采集逻辑（只存已收线、只追加、补缺、修订、失败不写数据）与来源无关，换来源时只需要换适配器。下面是 Webull 适配器的用法，留作参考。

`collect` 只调用行情接口，代码里没有任何交易接口。

1. 在项目根目录建 `.env`（不进 Git）：

   ```
   WEBULL_APP_KEY=...
   WEBULL_APP_SECRET=...
   WEBULL_REGION=us
   ```

2. 先核实账号能取到 ES 期货的 5 分钟线：

   ```bash
   uv run pytest -m webull_live -s
   ```

   2026-11-22 实测结果是 `MARKET_DATA_NOT_SUBSCRIBED`。期货代码格式、时间戳口径等几项仍是按 SDK 文档假定的，见 `docs/data-contract.md` 的"Webull 来源"。

3. 手工运行 `uv run python -m trading_agent collect`，检查 `~/trading-agent-data/webull/ES/` 下的 CSV。

### 每 5 分钟自动运行

`ops/launchd/collect.plist.example` 是一份 launchd 样例，在每个 5 分钟整点后 10 秒运行一次 `collect`。**它没有被安装。** 启用调度需要你自己决定并执行：

```bash
cp ops/launchd/collect.plist.example ~/Library/LaunchAgents/com.trading-agent.collect.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trading-agent.collect.plist
# 停用
launchctl bootout gui/$(id -u)/com.trading-agent.collect
```

等价的手工命令见 `ops/launchd/run-collect.sh`。

## 测试

```bash
uv run pytest                    # 默认：合成数据
uv run pytest -m realdata        # 需要数据根目录里的真实 Databento 文件，且已运行过 ingest
uv run pytest -m webull_live -s  # 需要 Webull 凭证并联网
```

合成样本在 `tests/fixtures/`，由 `tests/fixtures/make_fixtures.py` 生成，全部带 `FIXTURE` 标记。合成结果只用于验收，不能作为策略证据。

策略的逐 bar 向量在 `tests/vectors/strategy/pa_brooks/`，成交向量在 `tests/vectors/fills/`。每个向量会正向跑一遍，再按价格翻转跑一遍镜像。

## 需要你填写的配置

`config/risk.yaml` 里的金额类字段默认留空：每手费用、研究风险单位、账户的单笔预算、日损上限、最大手数。没填之前纸面评测照常运行（费用按 0 计并打标记），人工计划的预算核验一律不放行。填好账户限额后把 `account.approved` 改为 `true`。
