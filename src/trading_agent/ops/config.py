"""config/ops.yaml 与 config/control.yaml 的加载。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import ConfigError

LEVELS = ("CRITICAL", "WARN", "INFO")


@dataclass(frozen=True)
class OpsConfig:
    scopes: tuple[str, ...]
    lock_ttl_seconds: int
    late_bars_warn: int
    late_bars_critical: int
    grace_seconds: int
    after_close_minutes: int
    account: str | None
    notifier: str
    silence: dict
    push_levels: tuple[str, ...]
    explain_model: str
    explain_timeout: int
    explain_batch: int

    def silence_for(self, kind: str) -> int:
        return int(self.silence.get(kind, self.silence.get("default", 1800)))


@dataclass(frozen=True)
class ControlConfig:
    token_ttl_seconds: int
    cli_enabled: bool
    cli_users: dict
    telegram_enabled: bool
    telegram_chat_id: str | None
    telegram_users: dict
    telegram_users_from_secrets: bool = False
    telegram_bound_approver: bool = False

    def users(self, channel: str) -> dict:
        return {"CLI": self.cli_users, "TELEGRAM": self.telegram_users}.get(channel, {})


def _int(v, path, lo=0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ConfigError(f"{path} 应为 ≥{lo} 的整数：{v!r}")
    return v


def _yaml(project_root, name) -> dict:
    p = Path(project_root) / "config" / name
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"缺少 {p}") from None


def parse_ops(raw: dict) -> OpsConfig:
    t, d, a, e = (raw.get(k) or {} for k in ("tick", "daily", "alerts", "explain"))
    scopes = raw.get("scopes") or []
    if not scopes or any(not isinstance(s, str) or s.count(":") != 3 for s in scopes):
        raise ConfigError("ops.scopes 应为四维 scope 列表")
    if a.get("notifier") not in ("telegram", "none"):
        raise ConfigError("ops.alerts.notifier 只能是 telegram 或 none")
    lv = a.get("push_levels") or []
    if any(x not in LEVELS for x in lv):
        raise ConfigError(f"ops.alerts.push_levels 只能取 {LEVELS}")
    sil = a.get("silence_seconds") or {}
    for k, v in sil.items():
        _int(v, f"ops.alerts.silence_seconds.{k}")
    warn, crit = _int(t.get("late_bars_warn", 1), "ops.tick.late_bars_warn", 1), _int(t.get("late_bars_critical", 2), "ops.tick.late_bars_critical", 1)
    if crit < warn:
        raise ConfigError("ops.tick.late_bars_critical 不能小于 late_bars_warn")
    return OpsConfig(
        scopes=tuple(scopes), lock_ttl_seconds=_int(t.get("lock_ttl_seconds", 240), "ops.tick.lock_ttl_seconds", 30),
        late_bars_warn=warn, late_bars_critical=crit, grace_seconds=_int(t.get("grace_seconds", 90), "ops.tick.grace_seconds"),
        after_close_minutes=_int(d.get("after_close_minutes", 15), "ops.daily.after_close_minutes"),
        account=d.get("account"), notifier=a["notifier"], silence=dict(sil), push_levels=tuple(lv),
        explain_model=str(e.get("model") or "sonnet"), explain_timeout=_int(e.get("timeout_seconds", 180), "ops.explain.timeout_seconds", 10),
        explain_batch=_int(e.get("batch", 10), "ops.explain.batch", 1))


def parse_control(raw: dict) -> ControlConfig:
    ad = raw.get("adapters") or {}
    cli, tg = ad.get("cli") or {}, ad.get("telegram") or {}

    def users(m, path):
        out = {}
        for k, v in (m or {}).items():
            if not isinstance(v, dict):
                raise ConfigError(f"{path}.{k} 应为 {{approver: true/false}}")
            out[str(k)] = {"approver": bool(v.get("approver"))}
        return out
    from_secrets = tg.get("users") == "from_secrets"
    tg_users = {} if from_secrets else users(tg.get("users"), "control.adapters.telegram.users")
    if tg.get("enabled") and (not tg.get("chat_id") or not (tg_users or from_secrets)):
        raise ConfigError("启用 Telegram 控制通道要填 chat_id（或 from_env）和绑定的用户（或 from_secrets）")
    return ControlConfig(token_ttl_seconds=_int(raw.get("token_ttl_seconds", 120), "control.token_ttl_seconds", 10),
                         cli_enabled=bool(cli.get("enabled", True)), cli_users=users(cli.get("users"), "control.adapters.cli.users"),
                         telegram_enabled=bool(tg.get("enabled")),
                         telegram_chat_id=None if tg.get("chat_id") is None else str(tg.get("chat_id")),
                         telegram_users=tg_users, telegram_users_from_secrets=from_secrets,
                         telegram_bound_approver=bool(tg.get("approver", False)))


def load_ops(project_root) -> OpsConfig:
    return parse_ops(_yaml(project_root, "ops.yaml"))


def load_control(project_root) -> ControlConfig:
    return parse_control(_yaml(project_root, "control.yaml"))
