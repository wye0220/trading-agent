"""控制通道的适配器。各自负责校验来源；凭证各自独立，不打印、不写日志。

- 本机命令行：发送者 = 当前系统用户，须在 config/control.yaml 的 cli.users 里绑定。
- Telegram：专用机器人（token 在 <data_root>/secrets/control-telegram.env），只认登记的聊天和绑定的用户 ID。
"""
from __future__ import annotations

import getpass
import json
import urllib.parse
import urllib.request
from typing import Callable

from ..config import Settings
from . import state
from .config import ControlConfig
from .control import Message

OFFSET_KEY = "control:telegram_offset"
TOKEN_KEY = "CONTROL_TELEGRAM_BOT_TOKEN"


def cli_message(text: str, user: str | None = None) -> Message:
    return Message("CLI", user or getpass.getuser(), text, None, verified=True)


def telegram_token(settings: Settings) -> str | None:
    p = settings.data_root / "secrets" / "control-telegram.env"
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        k, _, v = line.partition("=")
        if k.strip().removeprefix("export ").strip() == TOKEN_KEY:
            return v.strip().strip("\"'") or None
    return None


def telegram_transport(token: str) -> Callable[[str, dict], dict]:
    def call(method: str, params: dict) -> dict:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    return call


def telegram_poll(con, ccfg: ControlConfig, transport: Callable[[str, dict], dict]) -> list[Message]:
    """拉取新消息。来自登记聊天、绑定用户的标为已校验；其他照样返回（会被拒绝并写审计）。"""
    offset = state.get(con, OFFSET_KEY) or 0
    r = transport("getUpdates", {"offset": offset + 1 if offset else 0, "timeout": 0})
    out = []
    last = offset
    for u in r.get("result") or []:
        last = max(last, int(u.get("update_id") or 0))
        m = u.get("message") or {}
        text = m.get("text")
        if not text:
            continue
        chat_ok = str((m.get("chat") or {}).get("id")) == str(ccfg.telegram_chat_id)
        uid = str((m.get("from") or {}).get("id"))
        out.append(Message("TELEGRAM", uid, text, f"tg:{u.get('update_id')}",
                           verified=chat_ok and uid in ccfg.telegram_users))
    if last != offset:
        state.put(con, OFFSET_KEY, last)
    return out


def telegram_reply(ccfg: ControlConfig, transport, text: str) -> None:
    transport("sendMessage", {"chat_id": ccfg.telegram_chat_id, "text": text[:3500]})
