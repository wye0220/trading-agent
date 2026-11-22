# Proposal

## Why

`trading-agent` 仓库目前只有 LICENSE、README 和 OpenSpec 配置，没有任何代码。《trading-agent · Claude 提示词包 v2.6 · PA 短窗口版》（`~/Downloads/trading_agent_claude_v2_6_pa.md`）要求按阶段建设，且"每次只实施点名的阶段"；它的首次发送指令是"把 Prompt 0 写入 CLAUDE.md 并执行 Prompt 1"。后面的策略、引擎、扫描和回放（阶段 2–5）全部依赖阶段 1 的数据合同和运行表，所以本次变更只做 **Prompt 0 + Prompt 1**。

与提示词包不同的三点，均来自用户：

1. **历史行情已有真实数据**。`data/GLBX-20260905-AKYFXCHGY4/` 是 Databento GLBX.MDP3 的 `ES.FUT` 1 分钟 OHLCV，2019-09-05 至 2026-09-04。阶段 1 不止用合成数据验收，还要能直接读它。
2. **每日新数据来自 Webull**。每 5 分钟从 Webull 只读取一次已收线的 K 线，存成 CSV，再导入。本项目自己实现这个采集，不依赖、不调用 `~/trading-desk` 的代码。
3. **行情数据移出项目目录**。现有的 Databento 文件、以后的 Webull CSV 和运行数据库都放到项目外的数据根目录，长期留在那里。

## What Changes

- 把 Prompt 0 写成精简的项目根 `CLAUDE.md`，并把提示词包原文放进仓库供后续阶段引用。
- 搭建 Python 3.12 项目骨架：`config/`、`src/trading_agent/`（含 `strategies/` 子包）、`tests/`（含 `vectors/strategy/`、`vectors/fills/`）、`data/`、`reports/`、`prompts/`、`schemas/`、`candidates/`、`incidents/`、`memory/`、`releases/`、`docs/`；原始行情、数据库、凭证排除在 Git 外。
- 新增配置：`config/instruments.yaml`（ES、NQ、MES、MNQ 合约规格与版本化换月规则）、`config/paths.yaml`（数据目录与项目外保留集路径登记）、`config/watch.yaml`（轮询间隔预设）。
- 把 `data/GLBX-20260905-AKYFXCHGY4/` 移到项目外的数据根目录（默认 `~/trading-agent-data/`），项目内不再存放真实行情。
- 新增行情只读导入：
  - **Databento DBN 适配器**，读数据根目录下的 `ohlcv-1m` 文件；只保留单腿合约，丢弃价差合约；把重复使用的短代码（如 `ESH0`）解析成唯一的实际合约。
  - **CSV 适配器**，字段与数据合同一致，用于 Webull 采集结果和合成样本。
  - 每次 `ingest` 扫描数据根目录，只导入新增或有变化的内容。
- 新增 **Webull 只读采集**（子命令 `collect`）：每次运行取实际 ES 合约最近已收线的 5 分钟线，按交易日追加到 CSV，记录真实的取数时刻；停机后再运行会补回 Webull 仍能提供的近期缺失。只调用行情接口，不含任何交易接口。
- 由 1 分钟线派生 5 分钟线、已完结 1 小时线、过去完整 RTH 日的高低差（供 ADR），以及 PDH、PDL、ONH、ONL、上一 RTH 收盘；每个派生结果记录依赖的 bar、revision 和 `known_at_utc`。
- 数据校验：实际合约、时间轴、重复与冲突、缺口、OHLC 一致性、交易日历与提前收盘、Databento `condition.json` 标出的降级日。异常时阻止该 scope 新入场。
- 运行 SQLite 库（WAL + `busy_timeout`）与独立的发布数据库，建好提示词包列出的全部表。
- CLI `python -m trading_agent`：实现 `ingest`、`collect` 和最小 `scan`（只做数据校验），其余子命令返回 `NOT_IMPLEMENTED`；支持 `--scope`，默认 `pa_brooks:ES:RTH:5m`。`collect` 是提示词包 13 个子命令之外新增的一个。
- 数据可得性报告 `reports/data_availability.md`，含可封存的 20 日胶囊和连续 40 日确认段的数量。
- 文档 `docs/glossary.md`、`docs/data-contract.md`，以及带 `FIXTURE` 标记的合成样本和测试。

不在本次范围：策略插件与 PA 规则（阶段 2）、引擎与成交模型（阶段 3）、`plan`/`watch`/纸面账本（阶段 4）、回放与基线（阶段 5）、二期全部内容、读取持仓和订单的券商接口、任何实盘下单能力。每 5 分钟自动运行 `collect` 的 launchd 调度只给出样例文件，不安装；按提示词包阶段 10 的规定，启用调度要用户另行授权。

## Capabilities

### New Capabilities

- `agent-cli`: `python -m trading_agent` 命令行入口、子命令清单、四维 scope 参数，以及未实现子命令的统一返回。
- `market-data-ingest`: 行情只读导入。涵盖 K 线数据合同、Databento DBN 与 CSV 两种来源、项目外数据根目录的增量导入、实际合约解析、revision 与可用时点、多来源取舍、幂等重跑。
- `live-bar-collection`: 从 Webull 只读采集已收线的 K 线并落盘为 CSV，含补缺、凭证处理和只读保证。
- `market-data-validation`: 行情校验规则、异常分类，以及数据异常对 scope 新入场的阻止。
- `derived-market-context`: 由已收线数据派生的背景：5 分钟线、1 小时线、RTH 日高低差、参考价位，连同它们的因果可见性（`known_at`）规则。
- `runtime-storage`: 运行数据库与发布数据库的表、隔离、并发与幂等保证。
- `data-availability-report`: 按实际合约汇总覆盖范围、缺口、预热情况和可分配盲测窗口的报告。

### Modified Capabilities

无。`openspec/specs/` 目前为空。

## Impact

- **代码**：全部为新增，位于 `src/trading_agent/`、`config/`、`tests/`、`docs/`、`reports/`。现有文件只改 `README.md`，并新增 `.gitignore`、`CLAUDE.md`、`pyproject.toml`、`.claude/settings.json`。
- **依赖**：Python 3.12（本机只有 3.14，用 `uv` 管理解释器和虚拟环境）、`databento-dbn`（本地解码，不联网、不需要 API key）、`webull-openapi-python-sdk`（只用行情部分）、`exchange_calendars`（锁定版本）、`PyYAML`、`pytest`。
- **数据**：真实行情和运行数据库在项目外的 `~/trading-agent-data/`；只存指针和审计事件的发布数据库留在项目的 `releases/`。现有 DBN 文件 221 MB，3,945,840 条记录，172 个代码中 40 个是单腿合约、132 个是价差。
- **凭证**：Webull OpenAPI 的 `WEBULL_APP_KEY`、`WEBULL_APP_SECRET` 放在项目根 `.env`，不进 Git，不写入日志。
- **与 `~/trading-desk` 的关系**：不读取、不依赖、不修改。它的 `pa-report` 里有一段 Webull 取数写法可作参考，但它对期货用的是 SPY 代理，本项目要取实际 ES 合约。
- **前提待核实**：用户的 Webull OpenAPI 账号能否取到 ES 期货的 5 分钟线，还没有验证。tasks.md 把这项验证排在采集实现之前；取不到就停下来报告。
