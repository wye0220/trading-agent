"""控制通道的适配器。各自负责校验来源；凭证各自独立，不打印、不写日志。

- 本机命令行：发送者 = 当前系统用户，须在 config/control.yaml 的 cli.users 里绑定。
- Telegram：先找专用机器人（<data_root>/secrets/control-telegram.env），没有就用通知用的共享机器人
  （与 ops/notify-telegram.sh 相同：只解析 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID 两个键）。只认登记的聊天里、
  绑定用户发的消息；绑定的用户 ID 存在 <data_root>/secrets/control-telegram.env，不进仓库。
"""
from __future__ import annotations

import dataclasses
import getpass
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from ..config import Settings
from . import state
from .config import ControlConfig
from .control import Message

OFFSET_KEY = "control:telegram_offset"
BIND_KEY = "control:telegram_bind_code"
SECRETS_FILE = "control-telegram.env"
# Telegram 的命令名不能带 "-"：下划线写法换回系统的命令名
ALIASES = {"watch_interval": "watch-interval", "confirm_memory": "confirm-memory"}


def cli_message(text: str, user: str | None = None) -> Message:
    return Message("CLI", user or getpass.getuser(), text, None, verified=True)


def _read(path: Path) -> dict:
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            k = k.strip().removeprefix("export ").strip()
            if k and not k.startswith("#"):
                out[k] = v.strip().strip("\"'")
    return out


def _secrets(settings: Settings) -> Path:
    return settings.data_root / "secrets" / SECRETS_FILE


def shared_env_files(settings: Settings) -> list[Path]:
    """与 ops/notify-telegram.sh 相同的顺序（2026-11-23 用户同意复用 trading-desk 的机器人）。"""
    env = os.environ.get("TRADING_AGENT_TELEGRAM_ENV")
    return ([Path(env)] if env else []) + [settings.data_root / "secrets" / "telegram.env",
                                           Path.home() / "trading-desk" / ".env"]


def telegram_credentials(settings: Settings) -> tuple[str, str] | None:
    """(token, chat_id)。先找专用机器人（control-telegram.env），再找通知用的共享机器人。只解析这两个键。"""
    own = _read(_secrets(settings))
    if own.get("CONTROL_TELEGRAM_BOT_TOKEN") and own.get("CONTROL_TELEGRAM_CHAT_ID"):
        return own["CONTROL_TELEGRAM_BOT_TOKEN"], own["CONTROL_TELEGRAM_CHAT_ID"]
    for f in shared_env_files(settings):
        d = _read(f)
        if d.get("TELEGRAM_BOT_TOKEN") and d.get("TELEGRAM_CHAT_ID"):
            return d["TELEGRAM_BOT_TOKEN"], d["TELEGRAM_CHAT_ID"]
    return None


def telegram_token(settings: Settings) -> str | None:
    c = telegram_credentials(settings)
    return c[0] if c else None


def bound_users(settings: Settings) -> list[str]:
    raw = _read(_secrets(settings)).get("CONTROL_TELEGRAM_USERS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def resolve(settings: Settings, ccfg: ControlConfig) -> ControlConfig:
    """把 chat_id: from_env、users: from_secrets 换成实际值（不写回配置文件，个人 ID 不进仓库）。"""
    chat, users = ccfg.telegram_chat_id, dict(ccfg.telegram_users)
    if chat == "from_env":
        c = telegram_credentials(settings)
        chat = c[1] if c else None
    if ccfg.telegram_users_from_secrets:
        users = {u: {"approver": ccfg.telegram_bound_approver} for u in bound_users(settings)}
    return dataclasses.replace(ccfg, telegram_chat_id=chat, telegram_users=users)


def normalize(text: str) -> str:
    """群里的命令以 / 开头，可能带 @机器人名；下划线写法换回系统命令名。"""
    t = (text or "").strip()
    if not t.startswith("/"):
        return t
    head, _, rest = t[1:].partition(" ")
    head = head.split("@", 1)[0].lower()
    head = ALIASES.get(head, head)
    return (head + (" " + rest if rest else "")).strip()


def telegram_transport(token: str) -> Callable[[str, dict], dict]:
    def call(method: str, params: dict) -> dict:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    return call


def _updates(con, transport) -> tuple[list[dict], int, bool]:
    offset = state.get(con, OFFSET_KEY)
    r = transport("getUpdates", {"offset": offset + 1 if offset else 0, "timeout": 0})
    ups = r.get("result") or []
    last = max([offset or 0] + [int(u.get("update_id") or 0) for u in ups])
    return ups, last, offset is None


def telegram_poll(con, ccfg: ControlConfig, transport: Callable[[str, dict], dict]) -> list[Message]:
    """拉取新消息。来自登记聊天、绑定用户的标为已校验；其他照样返回（会被拒绝并写审计）。

    第一次拉取（还没有记过位置）只记下位置、不执行任何消息：启用之前积压的消息不当命令。
    """
    ups, last, first = _updates(con, transport)
    out = []
    if not first:
        for u in ups:
            m = u.get("message") or {}
            text = m.get("text")
            if not text:
                continue
            chat_ok = str((m.get("chat") or {}).get("id")) == str(ccfg.telegram_chat_id)
            uid = str((m.get("from") or {}).get("id"))
            out.append(Message("TELEGRAM", uid, normalize(text), f"tg:{u.get('update_id')}",
                               verified=chat_ok and uid in ccfg.telegram_users))
    if first or last != (state.get(con, OFFSET_KEY) or 0):
        state.put(con, OFFSET_KEY, last)
    return out


def telegram_reply(ccfg: ControlConfig, transport, text: str) -> None:
    transport("sendMessage", {"chat_id": ccfg.telegram_chat_id, "text": text[:3500]})


def bind(settings: Settings, con, ccfg: ControlConfig, transport, *, wait_seconds: int = 180, sleep=time.sleep,
         code: str | None = None, emit=print) -> dict:
    """一次性绑定：在登记的群里发 /bind <码> 的那个人的用户 ID 写进 <data_root>/secrets/control-telegram.env。

    等待期间收到的其他消息不执行（只跳过）。码只在这次等待里有效。
    """
    code = code or secrets.token_hex(3)
    try:
        name = (transport("getMe", {}).get("result") or {}).get("username")
    except Exception:  # noqa: BLE001
        name = None
    emit(f"请在 Telegram 群里发：/bind{'@' + name if name else ''} {code}（{wait_seconds // 60} 分钟内有效）")
    state.put(con, BIND_KEY, {"code": code, "expires_at_utc": int(time.time()) + wait_seconds})
    deadline = time.time() + wait_seconds
    if state.get(con, OFFSET_KEY) is None:                 # 先记下位置，之前的消息不算
        _, last, _ = _updates(con, transport)
        state.put(con, OFFSET_KEY, last)
    skipped = 0
    while time.time() < deadline:
        ups, last, _ = _updates(con, transport)
        found = None
        for u in ups:
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(ccfg.telegram_chat_id):
                skipped += 1
                continue
            if normalize(m.get("text") or "") == f"bind {code}":
                found = str((m.get("from") or {}).get("id"))
            else:
                skipped += 1
        state.put(con, OFFSET_KEY, last)
        if found:
            users = sorted(set(bound_users(settings)) | {found})
            p = _secrets(settings)
            p.parent.mkdir(parents=True, exist_ok=True)
            kept = [ln for ln in (p.read_text(encoding="utf-8").splitlines() if p.is_file() else [])
                    if not ln.strip().removeprefix("export ").startswith("CONTROL_TELEGRAM_USERS")]
            p.write_text("\n".join(kept + [f"CONTROL_TELEGRAM_USERS={','.join(users)}"]) + "\n", encoding="utf-8")
            os.chmod(p, 0o600)
            state.put(con, BIND_KEY, None)
            telegram_reply(ccfg, transport, "trading-agent 控制通道：已绑定发送这条消息的用户。之后只有绑定的用户发的命令才执行。")
            return {"status": "BOUND", "user_id_suffix": found[-3:], "users": len(users), "skipped": skipped}
        sleep(3)
    state.put(con, BIND_KEY, None)
    return {"status": "TIMEOUT", "skipped": skipped}
