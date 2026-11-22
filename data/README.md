# data/

这个目录不存放真实行情。

真实行情和运行数据库在项目之外的数据根目录，默认 `~/trading-agent-data/`（可用环境变量
`TRADING_AGENT_DATA_ROOT` 覆盖，见 `config/paths.yaml`）：

```
~/trading-agent-data/
  databento/GLBX-20260905-AKYFXCHGY4/     Databento 历史文件（ES.FUT ohlcv-1m）
  webull/ES/<contract_id>/<交易日>.csv    collect 每 5 分钟追加
  runtime/trading_agent.sqlite            运行库
```

合成样本在 `tests/fixtures/`。详见 `docs/data-contract.md`。
