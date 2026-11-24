"""控制通道：交易员（值班工程师）的命令。

只有适配器校验过来源、发送者在 config/control.yaml 绑定的消息才执行；只认白名单里的命令，消息里其余文字只当数据。
每条消息（包括被拒绝的）都写进只追加的 control_commands。

二次确认：unfreeze 先返回短期令牌，令牌绑定原请求、通道和发送者；过期或重复使用不改变任何状态。
approve 要回显 manifest_hash。context 只能收紧风险。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime

from .. import sessions
from ..config import ConfigError, Settings, parse_scope
from ..scan import _session_for
from . import state
from .config import ControlConfig, OpsConfig

WHITELIST = ("plan", "activate", "watch-interval", "note", "context", "freeze", "unfreeze", "approve", "reject",
             "confirm-memory", "confirm")
APPROVER_ONLY = ("approve", "reject", "unfreeze", "confirm-memory")
INTERVALS = {"1m": 60, "5m": 300, "60": 60, "300": 300}


@dataclass(frozen=True)
class Message:
    channel: str                 # CLI | TELEGRAM | TEST
    user_id: str
    text: str
    message_ref: str | None = None
    verified: bool = False       # 适配器是否校验过来源（Telegram：聊天和发送者；本机：系统用户）


class ControlError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _audit(con, cid, msg: Message, command, args, status, reason, result, now):
    with con:
        con.execute("INSERT INTO control_commands VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cid, msg.channel, msg.user_id, msg.message_ref, command, json.dumps(args, ensure_ascii=False),
                     status, reason, json.dumps(result, ensure_ascii=False, default=str), now))


def _actor(msg: Message) -> str:
    return f"control:{msg.channel.lower()}:{msg.user_id}"


def handle(settings: Settings, con: sqlite3.Connection, msg: Message, ccfg: ControlConfig, ocfg: OpsConfig, *,
           registry, release_guard=None, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    if msg.message_ref:
        old = con.execute("SELECT command_id, status, reason, result_json FROM control_commands WHERE channel=? AND"
                          " message_ref=?", (msg.channel, msg.message_ref)).fetchone()
        if old is not None:
            return {"status": "DUPLICATE", "command_id": old[0], "previous_status": old[1]}
    cid = "ctl_" + hashlib.sha256(f"{msg.channel}|{msg.user_id}|{msg.message_ref}|{msg.text}|{now}|{secrets.token_hex(4)}"
                                  .encode()).hexdigest()[:16]
    tokens = msg.text.strip().split()
    command = tokens[0].lower() if tokens else ""
    args = tokens[1:]
    user = ccfg.users(msg.channel).get(str(msg.user_id))
    try:
        if not msg.verified or user is None:
            raise ControlError("UNAUTHORIZED", "来源没有通过校验，或发送者没有绑定：命令不执行")
        if command not in WHITELIST:
            raise ControlError("UNKNOWN_COMMAND", f"不在命令白名单里：{command or '（空）'}。消息只当数据，不执行")
        if command in APPROVER_ONLY and not user["approver"]:
            raise ControlError("NOT_APPROVER", f"{command} 只有批准人能用")
        result = HANDLERS[command](settings, con, msg, args, ccfg, ocfg, registry=registry, cid=cid, now=now,
                                   release_guard=release_guard)
    except ControlError as e:
        out = {"status": "REFUSED", "reason": e.reason, "message": str(e), "command_id": cid}
        _audit(con, cid, msg, command or None, args, "REFUSED", e.reason, {"message": str(e)}, now)
        return out
    except Exception as e:  # noqa: BLE001 — 命令执行出错：记审计，不重试
        reason = getattr(e, "reason", None) or type(e).__name__
        out = {"status": "REFUSED", "reason": reason, "message": str(e), "command_id": cid}
        _audit(con, cid, msg, command, args, "ERROR", reason, {"message": str(e)}, now)
        return out
    st = result.pop("_status", "DONE")
    tok = result.pop("_token", None)                  # 令牌不进审计正文
    _audit(con, cid, msg, command, args, st, None, result, now)
    if "status" in result:                            # 被调用功能自己的状态（如 ACTIVATED）不覆盖命令状态
        result["result_status"] = result.pop("status")
    out = {**result, "status": st, "command_id": cid}
    if tok:
        out["_token"], out["token"] = tok, tok[0]     # 只给发送者，不进审计
        out["text"] = out.get("text", "").replace("<令牌>", tok[0])
    return out


# ---------------------------------------------------------------- 命令
def _cli_json(settings, registry, argv: list[str]) -> tuple[int, dict]:
    """复用命令行的同一套校验：在本进程里跑一遍 CLI，取 JSON 输出。"""
    from ..cli import main
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main([*argv, "--json"], settings=settings, registry=registry)
    try:
        return code, json.loads(buf.getvalue())
    except ValueError:
        return code, {"output": buf.getvalue()[-500:]}


def _plan(settings, con, msg, args, ccfg, ocfg, *, registry, **_):
    if len(args) < 4:
        raise ControlError("BAD_ARGS", "用法：plan ES L 5000 4995 5010 1 理由…（阶段 4 的一行快捷语法）")
    code, r = _cli_json(settings, registry, ["plan", *args] + (["--account", ocfg.account] if ocfg.account else []))
    if code != 0:
        raise ControlError("PLAN_FAILED", r.get("message") or "预检失败")
    return {"plan_id": r.get("plan_id"), "conclusion": (r.get("check") or {}).get("conclusion"),
            "text": (r.get("check") or {}).get("summary")}


def _activate(settings, con, msg, args, ccfg, ocfg, *, registry, **_):
    if len(args) != 1:
        raise ControlError("BAD_ARGS", "用法：activate <plan_id>")
    code, r = _cli_json(settings, registry, ["plan", "--activate", args[0]])
    if r.get("status") == "ERROR":
        raise ControlError("ACTIVATE_FAILED", r.get("message") or "激活失败")
    return {"plan_id": args[0], "plan_status": r.get("status"), "text": (r.get("check") or {}).get("summary")}


def _watch_interval(settings, con, msg, args, ccfg, ocfg, *, now, **_):
    if len(args) != 1 or args[0].lower() not in INTERVALS:
        raise ControlError("BAD_ARGS", "用法：watch-interval 60|300|1m|5m")
    sec = INTERVALS[args[0].lower()]
    if sec not in (settings.watch.get("presets") or [60, 300]):
        raise ControlError("BAD_ARGS", f"{sec} 秒不在 config/watch.yaml 的 presets 里")
    state.put(con, "watch_interval_override", {"seconds": sec, "by": _actor(msg)}, now)
    return {"seconds": sec, "text": f"watch 下一轮起按 {sec} 秒轮询"}


def _note(settings, con, msg, args, ccfg, ocfg, **_):
    from ..manual import discipline
    if not args:
        raise ControlError("BAD_ARGS", "用法：note [事件标识] 纠正内容…")
    event_id = args[0] if re.match(r"^[a-z]+_[0-9a-f]{8,}$", args[0]) else None
    body = " ".join(args[1:] if event_id else args)
    try:
        nid = discipline.add_note(con, event_id, body, _actor(msg))
    except ValueError as e:
        raise ControlError("BAD_ARGS", str(e)) from None
    return {"note_id": nid, "event_id": event_id}


def _scopes(settings, ocfg, target: str) -> list:
    if target.upper() == "ALL":
        return [parse_scope(s, settings) for s in ocfg.scopes]
    try:
        return [parse_scope(target, settings)]
    except ConfigError as e:
        raise ControlError("BAD_ARGS", str(e)) from None


def _until(now: int, hhmm: str) -> int:
    m = re.match(r"^(\d{1,2}):(\d{2})$", hhmm)
    if not m:
        raise ControlError("BAD_ARGS", f"时刻格式应为 HH:MM（纽约时间）：{hhmm}")
    local = sessions.et(now)
    t = datetime.combine(local.date(), dtime(int(m.group(1)), int(m.group(2))), local.tzinfo)
    u = sessions.ts(t)
    if u <= now:
        raise ControlError("BAD_ARGS", f"{hhmm} 已经过了")
    return u


def _context(settings, con, msg, args, ccfg, ocfg, *, cid, now, **_):
    """context no-entry [until HH:MM] [scope|ALL]；context max-qty N [until HH:MM] [scope|ALL]。缺省到当日收盘、ALL。"""
    from ..release import freeze as rfreeze
    if not args or args[0] not in ("no-entry", "max-qty"):
        raise ControlError("BAD_ARGS", "用法：context no-entry [until HH:MM] [scope|ALL] 或 context max-qty N [until HH:MM] [scope|ALL]"
                                       "（只能收紧风险）")
    kind = "NO_ENTRY" if args[0] == "no-entry" else "MAX_QTY"
    rest = args[1:]
    value = {}
    if kind == "MAX_QTY":
        if not rest or not rest[0].isdigit() or int(rest[0]) < 1:
            raise ControlError("BAD_ARGS", "max-qty 后面要跟一个正整数")
        value = {"max_qty": int(rest[0])}
        rest = rest[1:]
    day, _ = _session_for(now)
    until = sessions.rth_bounds(day)[1]
    if len(rest) >= 2 and rest[0] == "until":
        until, rest = _until(now, rest[1]), rest[2:]
    target = rest[0] if rest else "ALL"
    if len(rest) > 1:
        raise ControlError("BAD_ARGS", "多余的参数：context 只认固定文法")
    scopes = _scopes(settings, ocfg, target)
    if until <= now:
        raise ControlError("BAD_ARGS", "约束的截止时刻已过")
    k = "ctx_" + hashlib.sha256(f"{cid}|{kind}|{target}".encode()).hexdigest()[:16]
    with con:
        con.execute("INSERT INTO context_constraints VALUES (?,?,?,?,?,?,?,?,?)",
                    (k, kind, "ALL" if target.upper() == "ALL" else str(scopes[0]), json.dumps(value), now, until,
                     _actor(msg), cid, now))
    frozen = []
    if kind == "NO_ENTRY":
        for s in scopes:
            frozen.append(rfreeze.freeze(con, s, "CONTEXT", f"当日约束 {k} 到 {sessions.iso(until)}", _actor(msg),
                                         domain=rfreeze.SCOPE_WIDE)["status"])
    return {"constraint_id": k, "kind": kind, "scope": target, "until_utc": until, "value": value,
            "text": f"{'禁止新入场' if kind == 'NO_ENTRY' else '手数上限 ' + str(value['max_qty'])}，到 {sessions.iso(until)}"}


def _freeze(settings, con, msg, args, ccfg, ocfg, **_):
    from ..release import freeze as rfreeze
    if not args:
        raise ControlError("BAD_ARGS", "用法：freeze <scope|ALL> [原因…]")
    reason = " ".join(args[1:]) or "控制通道冻结"
    out = [rfreeze.freeze(con, s, "MANUAL", reason, _actor(msg)) for s in _scopes(settings, ocfg, args[0])]
    return {"frozen": [{"scope": x["scope"], "status": x["status"], "freeze_event_id": x["freeze_event_id"]} for x in out]}


def _unfreeze(settings, con, msg, args, ccfg, ocfg, *, cid, now, **_):
    from ..release import freeze as rfreeze
    from ..release.config import load_release
    if len(args) != 2:
        raise ControlError("BAD_ARGS", "用法：unfreeze <scope> <freeze_event_id>")
    scope = _scopes(settings, ocfg, args[0])[0]
    ev = con.execute("SELECT cause, risk_domain FROM freeze_events WHERE freeze_event_id=? AND scope=? AND action='FREEZE'",
                     (args[1], str(scope))).fetchone()
    if ev is None:
        raise ControlError("NO_SUCH_FREEZE", f"{scope} 没有冻结事件 {args[1]}")
    rcfg = load_release(settings.project_root)
    if ev[0] not in rcfg.freeze["human_release"]:
        raise ControlError("POLICY", f"{ev[0]} 冻结不能由人工解除（政策见 config/release.yaml）")
    if not any(x["cause"] == ev[0] and x["risk_domain"] == ev[1] for x in rfreeze.active_causes(con, scope)):
        raise ControlError("NOT_FROZEN", f"{scope} 的 {ev[0]} 冻结已经解除")
    token = secrets.token_hex(4)
    action = {"action": "unfreeze", "scope": str(scope), "cause": ev[0], "risk_domain": ev[1], "freeze_event_id": args[1]}
    # 令牌要等这条命令写进审计之后才能引用它：先返回，由 handle 写审计，再插入令牌
    return {"_status": "PENDING_CONFIRM", "_token": (token, action),
            "expires_at_utc": now + ccfg.token_ttl_seconds,
            "text": f"确认解除 {scope} 的 {ev[0]} 冻结：{ccfg.token_ttl_seconds} 秒内发送 confirm <令牌>"}


def _confirm(settings, con, msg, args, ccfg, ocfg, *, now, **_):
    from ..release import freeze as rfreeze
    from ..release.config import load_release
    from ..release.guard import Approval
    if len(args) != 1:
        raise ControlError("BAD_ARGS", "用法：confirm <令牌>")
    r = con.execute("SELECT * FROM confirm_tokens WHERE token=?", (args[0],)).fetchone()
    if r is None:
        raise ControlError("INVALID_TOKEN", "没有这个令牌")
    if r["used_at_utc"] is not None:
        raise ControlError("TOKEN_USED", "令牌已经用过")
    if r["channel"] != msg.channel or r["user_id"] != str(msg.user_id):
        raise ControlError("TOKEN_MISMATCH", "令牌属于另一个发送者或通道")
    if now > r["expires_at_utc"]:
        raise ControlError("TOKEN_EXPIRED", "令牌已过期，状态没有改变")
    with con:
        c = con.execute("UPDATE confirm_tokens SET used_at_utc=? WHERE token=? AND used_at_utc IS NULL", (now, args[0]))
        if c.rowcount != 1:
            raise ControlError("TOKEN_USED", "令牌已经用过")
    a = json.loads(r["action_json"])
    scope = parse_scope(a["scope"], settings)
    res = rfreeze.unfreeze(con, scope, a["cause"], f"控制通道确认 {args[0]}", load_release(settings.project_root),
                           approval=Approval(_actor(msg), "CONTROL_CHANNEL"), domain=a["risk_domain"])
    return {"confirmed": a, "result": res["status"]}


def _approve(settings, con, msg, args, ccfg, ocfg, *, registry, release_guard, now, **_):
    from .. import db
    from ..engine.config import load_risk
    from ..release import run as rrun
    from ..release.config import load_release
    from ..release.guard import Approval, verify_release_code
    if len(args) not in (2, 3):
        raise ControlError("BAD_ARGS", "用法：approve <候选或baseline_id> <manifest_hash> [scope]（要回显完整的 manifest_hash）")
    target, h = args[0], args[1]
    if not re.match(r"^[0-9a-f]{64}$", h):
        raise ControlError("BAD_ARGS", "manifest_hash 要回显完整的 64 位摘要")
    scope = _scopes(settings, ocfg, args[2] if len(args) == 3 else ocfg.scopes[0])[0]
    rw = db.open_release(settings)
    try:
        r = rrun.approve(settings, con, rw, scope, target, h, Approval(_actor(msg), "CONTROL_CHANNEL"),
                         load_release(settings.project_root), registry=registry, risk_cfg=load_risk(settings.project_root),
                         guard=release_guard or verify_release_code, now=now)
    finally:
        rw.close()
    return {**r, "text": f"{r['status']}：{scope} 的 active 是 {r['release_id']}"}  # status 会移到 result_status


def _reject(settings, con, msg, args, ccfg, ocfg, *, now, **_):
    from ..memory import store as mem
    from ..release import run as rrun
    if not args:
        raise ControlError("BAD_ARGS", "用法：reject <release_id|memc_…> [原因]")
    target, reason = args[0], " ".join(args[1:]) or "REJECTED_BY_TRADER"
    if target.startswith("memc_"):
        mem.reject(con, target, reason="NOT_WANTED", actor=_actor(msg))
        return {"rejected": target, "kind": "memory_candidate"}
    row = rrun.version_row(con, target)
    if row is None or row["status"] != "PROPOSED":
        raise ControlError("NOT_PENDING", f"{target} 不是待批准的发布清单")
    with con:
        rrun._set_status(con, target, "ABANDONED", _actor(msg), {"reason": reason}, now)
    return {"rejected": target, "kind": "release_manifest"}


def _confirm_memory(settings, con, msg, args, ccfg, ocfg, **_):
    from ..memory import store as mem
    if len(args) != 1:
        raise ControlError("BAD_ARGS", "用法：confirm-memory <memc_…>")
    r = mem.confirm(settings, con, args[0], actor=_actor(msg))
    return {"candidate_id": args[0], "result": r.get("status") if isinstance(r, dict) else r}


HANDLERS = {"plan": _plan, "activate": _activate, "watch-interval": _watch_interval, "note": _note, "context": _context,
            "freeze": _freeze, "unfreeze": _unfreeze, "approve": _approve, "reject": _reject,
            "confirm-memory": _confirm_memory, "confirm": _confirm}


def execute(settings: Settings, con: sqlite3.Connection, msg: Message, ccfg: ControlConfig, ocfg: OpsConfig, *,
            registry, release_guard=None, now: int | None = None) -> dict:
    """handle 之上加令牌落库（令牌引用审计行，所以在审计写入之后插入）。"""
    now = int(time.time()) if now is None else now
    r = handle(settings, con, msg, ccfg, ocfg, registry=registry, release_guard=release_guard, now=now)
    tok = r.pop("_token", None)
    if tok:
        token, action = tok
        with con:
            con.execute("INSERT INTO confirm_tokens VALUES (?,?,?,?,?,?,NULL)",
                        (token, r["command_id"], msg.channel, str(msg.user_id), json.dumps(action),
                         now + ccfg.token_ttl_seconds))
    return r
