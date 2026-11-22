# trading-agent

ES、RTH 5 分钟、行情只读、纸面模拟。没有任何实盘下单功能。

规格来源是 `trading_agent_v2_6_pa.md`（Claude 提示词包 v2.6）。目前完成的是**阶段 1：数据合同与项目骨架**。术语见 `docs/glossary.md`，数据合同见 `docs/data-contract.md`。

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
uv run python -m trading_agent ingest --through 2025-12-31    # 晚于该交易日的 K 线不进运行库
uv run python -m trading_agent ingest --rebuild               # 改了换月规则等配置后：从原始 K 线重算派生数据和交易日状态

# 数据校验 + 封存快照。阶段 1 只会给出 WAIT 或 DATA_ERROR，不产生信号
uv run python -m trading_agent scan --as-of 2026-09-03T15:00:00Z
uv run python -m trading_agent scan --scope pa_brooks:ES:RTH:5m --json      # 缺省 as-of 为当前时刻

# 从 Webull 只读采集已收线的 5 分钟线，追加到 CSV 并入库
uv run python -m trading_agent collect
```

`--scope` 的格式是 `strategy_family:instrument_root:session_type:timeframe`，缺省 `pa_brooks:ES:RTH:5m`。

其余子命令（`plan`、`watch`、`replay`、`review`、`evolve`、`evaluate`、`shadow`、`release`、`rollback`、`freeze`、`monitor`）返回 `NOT_IMPLEMENTED`，由后续阶段实现。

## Webull 采集

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

   这一步还没有做过。期货代码格式、时间戳口径等几项是按 SDK 文档假定的，见 `docs/data-contract.md` 的"Webull 来源"。

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
