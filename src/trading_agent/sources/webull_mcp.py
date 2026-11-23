"""Webull MCP 连接器取回的期货 K 线：从保存下来的 JSON 文件读取。

MCP 连接器只能在 Claude 会话里调用，采集程序自己调不了它。做法是：会话里调用 get_futures_bars，
把返回的 JSON 原样存成文件，再由 collect --from-mcp 读这个文件。之后的处理（只存已收线、只追加、
修订、入库）与其他来源完全相同。这里没有任何网络请求，也没有任何交易接口。

文件内容是连接器的原始返回：[{"symbol": "ESZ6", "result": [{"time", "open", "high", "low", "close",
"volume"}, ...], "delay_minutes": 0}, ...]。time 是 K 线的起始时刻（2026-11-23 实测核对）。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .. import contracts, sessions
from .webull import FetchError


class McpDump:
    def __init__(self, path: Path | str, today: date):
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise FetchError("BAD_DUMP", f"读不了 {path}：{e}") from None
        if isinstance(raw, dict):
            raw = raw.get("response", raw.get("data"))
        if not isinstance(raw, list):
            raise FetchError("BAD_DUMP", "文件内容不是连接器返回的列表")
        self.entries: dict[str, dict] = {}
        for e in raw:
            if not isinstance(e, dict) or "symbol" not in e or not isinstance(e.get("result"), list):
                raise FetchError("BAD_DUMP", "条目缺少 symbol 或 result")
            try:
                cid = contracts.normalize(str(e["symbol"]), today)
            except contracts.ContractError:
                raise FetchError("BAD_DUMP", f"认不出合约代码 {e['symbol']!r}") from None
            if cid in self.entries:
                raise FetchError("BAD_DUMP", f"合约重复：{e['symbol']}")
            self.entries[cid] = e

    def roots(self) -> dict[str, str]:
        """文件里各合约的根代码 -> 规范合约标识。"""
        return {cid[:-5]: cid for cid in self.entries}

    def fetch(self, contract_id: str, timeframe: str, count: int, start_utc: int, now_utc: int):
        e = self.entries.get(contract_id)
        if e is None:
            have = "、".join(sorted(self.entries)) or "无"
            raise FetchError("CONTRACT_NOT_IN_DUMP", f"文件里没有 {contract_id}（有：{have}）")
        rows = []
        try:
            for r in e["result"]:
                rows.append({"start": sessions.parse_utc(r["time"]), "open": r["open"], "high": r["high"],
                             "low": r["low"], "close": r["close"], "volume": r["volume"]})
        except (KeyError, TypeError, ValueError) as ex:
            raise FetchError("BAD_DUMP", f"{e['symbol']} 的 K 线缺字段：{ex}") from None
        rows.sort(key=lambda r: r["start"])
        return e["symbol"], rows
