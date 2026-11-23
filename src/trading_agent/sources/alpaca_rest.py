"""Alpaca 模拟盘的只读网络访问。只发 GET，只连模拟盘地址；没有任何交易类请求。

凭证：环境变量 ALPACA_API_KEY、ALPACA_SECRET_KEY（或 APCA_API_KEY_ID、APCA_API_SECRET_KEY），其次项目根目录的 .env。
凭证不打印、不写日志；出错时的消息不带请求头。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .webull import read_env_file

PAPER_HOST = "paper-api.alpaca.markets"
# 全部是读取类端点
ENDPOINTS = {"positions": ("/v2/positions", {}),
             "orders_open": ("/v2/orders", {"status": "open", "nested": "false", "limit": "500"}),
             "orders_all": ("/v2/orders", {"status": "all", "nested": "false", "limit": "500", "direction": "desc"}),
             "fills": ("/v2/account/activities/FILL", {"page_size": "100", "direction": "desc"})}


class AlpacaError(Exception):
    pass


def credentials(project_root: Path, env: dict | None = None) -> tuple[str, str] | None:
    env = os.environ if env is None else env
    file = read_env_file(Path(project_root) / ".env")
    for k, s in (("ALPACA_API_KEY", "ALPACA_SECRET_KEY"), ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")):
        key, sec = env.get(k) or file.get(k), env.get(s) or file.get(s)
        if key and sec:
            return key, sec
    return None


class RestTransport:
    """只发 GET，只连模拟盘。密钥不打印、不写日志。"""

    def __init__(self, key: str, secret: str, timeout: int = 15):
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
        self._timeout = timeout

    def get(self, name: str):
        path, params = ENDPOINTS[name]
        url = f"https://{PAPER_HOST}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), int(time.time())
        except Exception as e:  # noqa: BLE001 — 网络和鉴权错误统一成读取失败；不带请求头
            raise AlpacaError(f"Alpaca 读取失败（{name}）：{type(e).__name__}") from None
