"""Webull OpenAPI 行情只读适配器。只用 SDK 的行情模块，不含任何交易接口。

凭证：环境变量 WEBULL_APP_KEY、WEBULL_APP_SECRET、WEBULL_REGION（默认 us），其次项目根目录的 .env。
凭证不打印、不写日志。SDK 默认会把每次请求（请求头含 app key）打到 stdout 和 ./webull_data_sdk.log，
这里在创建客户端前把这两处关掉。

注意：期货 K 线的返回格式、时间戳口径和回看上限尚未用真实凭证核实（见 docs/data-contract.md 的
「Webull 来源」）。解析按 SDK 文档和常见返回形状编写，核实后如有出入只需改 parse()。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import contracts
from ..bars import TIMEFRAME_SECONDS

SOURCE = "webull"
TIMESPAN = {"1m": "M1", "5m": "M5"}
CATEGORY = "US_FUTURES"
MAX_COUNT = 1200


class FetchError(Exception):
    """取数失败。message 不含凭证。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}{'：' + message if message else ''}")
        self.code = code


def read_env_file(path: Path) -> dict[str, str]:
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def credentials(env: dict | None, project_root: Path) -> tuple[str, str, str]:
    env = os.environ if env is None else env
    file_env = read_env_file(project_root / ".env")
    get = lambda k: env.get(k) or file_env.get(k) or ""   # noqa: E731
    key, secret = get("WEBULL_APP_KEY"), get("WEBULL_APP_SECRET")
    if not key or not secret:
        raise FetchError("CREDENTIALS_MISSING", "没有 WEBULL_APP_KEY / WEBULL_APP_SECRET（环境变量或项目根 .env）")
    return key, secret, get("WEBULL_REGION") or "us"


def _to_epoch(value) -> int:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()):
        n = int(value)
        return n // 1000 if n > 10_000_000_000 else n      # 毫秒或秒
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse(payload, timeframe: str) -> tuple[str | None, list[dict]]:
    """SDK 返回的 JSON → (返回的合约代码, [{start, open, high, low, close, volume}])。按起始时刻升序、去重。"""
    rows = payload
    symbol = None
    if isinstance(rows, dict):
        symbol = rows.get("symbol")
        rows = rows.get("result", rows.get("data", []))
    if rows and isinstance(rows, list) and isinstance(rows[0], dict) and "result" in rows[0]:
        symbol = rows[0].get("symbol", symbol)
        rows = rows[0]["result"]
    size = TIMEFRAME_SECONDS[timeframe]
    out = {}
    for r in rows or []:
        try:
            t = _to_epoch(r["time"])
            t -= t % size
            out[t] = {"start": t, "open": str(r["open"]), "high": str(r["high"]), "low": str(r["low"]),
                      "close": str(r["close"]), "volume": str(r.get("volume") or 0)}
        except (KeyError, ValueError, TypeError) as e:
            raise FetchError("BAD_RESPONSE", f"无法解析 K 线：{type(e).__name__}") from None
    return symbol, [out[t] for t in sorted(out)]


class WebullBars:
    """collect 使用的适配器接口：fetch(contract_id, timeframe, count, start_utc, end_utc)。"""

    name = SOURCE

    def __init__(self, env: dict | None = None, project_root: Path | None = None):
        self._env, self._root, self._dc = env, project_root, None

    def _client(self):
        if self._dc is None:
            key, secret, region = credentials(self._env, self._root)
            try:
                from webull.core.client import ApiClient
                from webull.data.data_client import DataClient
            except ImportError:
                raise FetchError("SDK_MISSING", "没装 webull-openapi-python-sdk") from None
            for name in ("webull", "webull.core", "webull.data"):
                lg = logging.getLogger(name)
                lg.handlers.clear()
                lg.addHandler(logging.NullHandler())
                lg.propagate = False
            api = ApiClient(key, secret, region)
            # 告诉 SDK 日志已配置，阻止它默认往 stdout 和本地文件写请求日志
            api._stream_logger_set = True
            api._file_logger_set = True
            self._dc = DataClient(api)
        return self._dc

    def fetch(self, contract_id: str, timeframe: str, count: int,
              start_utc: int | None = None, end_utc: int | None = None) -> tuple[str | None, list[dict]]:
        symbol = contracts.short_symbol(contract_id)
        dc = self._client()
        try:
            resp = dc.futures_market_data.get_futures_history_bars(
                [symbol], CATEGORY, TIMESPAN[timeframe], count=str(min(count, MAX_COUNT)),
                real_time_required=True,
                start_time=None if start_utc is None else start_utc * 1000,
                end_time=None if end_utc is None else end_utc * 1000)
            payload = resp.json()
        except FetchError:
            raise
        except Exception as e:  # noqa: BLE001  SDK 抛 ServerException(error_code=..., error_msg=...)
            code = str(getattr(e, "error_code", "") or type(e).__name__)
            raise FetchError(code, str(getattr(e, "error_msg", "") or "")[:200]) from None
        returned, rows = parse(payload, timeframe)
        return returned or symbol, rows
