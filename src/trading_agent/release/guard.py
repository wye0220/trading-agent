"""发布的两道校验：人工确认（谁在批准）和发布守卫（发布服务的代码是不是你签收过的那份）。

人工确认要求系统用户在批准人名单里、标准输入是交互终端、手动输入摘要前缀。它挡住无人值守脚本和代理的误操作，
但不是安全边界：同一系统用户下的程序可以伪造终端。真正的边界是发布身份隔离（见 docs/release-policy.md）。
"""
from __future__ import annotations

import getpass
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from .config import ReleaseConfig

RELEASE_PATHS = ("src/trading_agent/release", "config/release.yaml")
CHANNELS = ("CLI_TTY", "CONTROL_CHANNEL", "TEST")


class ReleaseError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Approval:
    """一次经身份校验的人工确认。channel=TEST 只在测试的临时发布库上被接受。"""
    actor: str
    channel: str


def release_digest(project_root: Path) -> str:
    h = hashlib.sha256()
    root = Path(project_root)
    for rel in RELEASE_PATHS:
        p = root / rel
        files = sorted(x for x in p.rglob("*") if x.is_file() and x.suffix in (".py", ".yaml")) if p.is_dir() else [p]
        for f in files:
            h.update(str(f.relative_to(root)).encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def verify_release_code(settings: Settings) -> dict:
    """批准和回滚之前：发布服务的代码摘要要与受保护的摘要清单一致。"""
    from ..evaluation.guard import manifest_path
    p = manifest_path(settings)
    try:
        man = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReleaseError("RELEASE_GUARD", f"没有受保护的摘要清单：{p}（由评测／发布身份写入）") from None
    except (OSError, ValueError) as e:
        raise ReleaseError("RELEASE_GUARD", f"摘要清单读不出来：{e}") from None
    now = release_digest(settings.project_root)
    if man.get("release_sha256") != now:
        raise ReleaseError("RELEASE_GUARD", "发布服务的代码与摘要清单不符：改动要先经你 review 并更新清单")
    return {"release_sha256": now, "accepted_by": man.get("accepted_by")}


def check_approval(settings: Settings, cfg: ReleaseConfig, approval: Approval | None) -> Approval:
    if not isinstance(approval, Approval) or approval.channel not in CHANNELS:
        raise ReleaseError("NO_HUMAN_APPROVAL", "没有经身份校验的人工批准")
    if approval.channel == "TEST":
        real = (settings.project_root / "releases" / "release-state.sqlite").resolve()
        if Path(settings.release_db).resolve() == real:
            raise ReleaseError("NO_HUMAN_APPROVAL", "测试通道不能写项目的发布库")
        return approval
    if approval.actor not in cfg.approvers:
        raise ReleaseError("NO_HUMAN_APPROVAL", f"{approval.actor} 不在批准人名单里（config/release.yaml）")
    return approval


def cli_confirm(cfg: ReleaseConfig, what: str, expected: str, *, stdin=None, out=None, user: str | None = None) -> Approval:
    """在交互终端里让批准人手动输入 expected 的前几位。不是终端、不是批准人、输错都拒绝。"""
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stderr
    user = user or getpass.getuser()
    if user not in cfg.approvers:
        raise ReleaseError("NO_HUMAN_APPROVAL", f"{user} 不在批准人名单里（config/release.yaml）")
    try:
        tty = stdin.isatty()
    except (AttributeError, ValueError):
        tty = False
    if not tty:
        raise ReleaseError("NO_HUMAN_APPROVAL", f"{what}要在交互终端里由你手动确认（标准输入不是终端）")
    n = cfg.confirm_prefix_chars
    out.write(f"{what}。确认请输入 {expected[:n]}（{n} 位）：")
    out.flush()
    typed = (stdin.readline() or "").strip()
    if typed != expected[:n]:
        raise ReleaseError("NO_HUMAN_APPROVAL", "输入与要求的前缀不符，没有执行")
    return Approval(user, "CLI_TTY")
