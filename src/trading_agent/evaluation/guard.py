"""评测的守卫：评测身份、受保护的摘要清单、隔离检查。

隔离本身靠操作系统权限（保留集目录归评测身份所有）。这里的代码只负责校验和拒绝，以及如实报告当前身份读不读得到。
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from ..config import Settings
from ..research.baseline import code_digest
from .config import EvalConfig

EVALUATOR_PATHS = ("src/trading_agent/evaluation", "src/trading_agent/incidents", "src/trading_agent/research/splits.py",
                   "src/trading_agent/research/metrics.py", "src/trading_agent/research/power.py",
                   "config/evaluation.yaml", "config/research.yaml")
CANARY = "CANARY"


class GuardError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def evaluator_digest(project_root: Path) -> str:
    h = hashlib.sha256()
    root = Path(project_root)
    for rel in EVALUATOR_PATHS:
        p = root / rel
        files = sorted(x for x in p.rglob("*") if x.is_file() and x.suffix in (".py", ".yaml")) if p.is_dir() else [p]
        for f in files:
            h.update(str(f.relative_to(root)).encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def digests(settings: Settings) -> dict:
    return {"evaluator_sha256": evaluator_digest(settings.project_root),
            "engine_sha256": code_digest(settings.project_root)}


def manifest_path(settings: Settings) -> Path:
    return settings.holdout_root / "protected" / "evaluator-manifest.json"


def current_user() -> str:
    return getpass.getuser()


def check_identity(cfg: EvalConfig, user: str | None = None) -> str:
    user = user or current_user()
    if user in cfg.author_users:
        raise GuardError("AUTHOR_IDENTITY", f"系统用户 {user} 是候选作者，不能评测真实窗口，也不能写摘要清单")
    return user


def accept(settings: Settings, cfg: EvalConfig, *, user: str | None = None, now: int | None = None) -> dict:
    """把当前代码的摘要写进受保护的清单。只有评测身份能做：这一步等于你逐行 review 之后的签收。"""
    who = check_identity(cfg, user)
    body = {**digests(settings), "accepted_by": who, "accepted_at_utc": int(time.time()) if now is None else now}
    p = manifest_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body, indent=1, sort_keys=True), encoding="utf-8")
    return body


def verify(settings: Settings, cfg: EvalConfig, *, user: str | None = None) -> dict:
    """评测真实窗口之前的校验。任一不满足就拒绝。"""
    who = check_identity(cfg, user)
    p = manifest_path(settings)
    try:
        man = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GuardError("MANIFEST_MISSING", f"没有受保护的摘要清单：{p}") from None
    except (OSError, ValueError) as e:
        raise GuardError("MANIFEST_MISSING", f"摘要清单读不出来：{e}") from None
    now = digests(settings)
    bad = [k for k in now if man.get(k) != now[k]]
    if bad:
        raise GuardError("DIGEST_MISMATCH", f"代码与摘要清单不符（{'、'.join(bad)}）：改动要先经你 review 并更新清单")
    return {"user": who, **now, "accepted_by": man.get("accepted_by"), "accepted_at_utc": man.get("accepted_at_utc")}


def _try(fn) -> str:
    try:
        fn()
        return "READABLE"
    except (PermissionError, FileNotFoundError, NotADirectoryError, OSError, subprocess.CalledProcessError):
        return "BLOCKED"


def isolation_check(settings: Settings, cfg: EvalConfig, *, user: str | None = None) -> dict:
    """以当前身份尝试读保留集目录。不输出任何文件内容，只报告每条路径是否被挡住。"""
    root = settings.holdout_root
    who = user or current_user()
    target = root / CANARY
    exists = os.path.lexists(root)

    def by_symlink():
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "link"
            link.symlink_to(root)
            os.listdir(link)

    def by_open():
        if target.is_file():
            with open(target, "rb") as f:
                f.read(1)
        else:
            os.listdir(root)

    paths = {
        "python_listdir": _try(lambda: os.listdir(root)),
        "python_open": _try(by_open),
        "shell_cat": _try(lambda: subprocess.run(["/bin/sh", "-c", 'ls "$1" >/dev/null && { [ ! -f "$2" ] || cat "$2" >/dev/null; }',
                                                  "sh", str(root), str(target)], check=True, capture_output=True)),
        "symlink": _try(by_symlink),
    }
    sealed = cfg.sealed_data_root or settings.data_root
    sealed_inside = root in sealed.resolve().parents or sealed.resolve() == root
    is_author = who in cfg.author_users
    problems = []
    if not exists:
        problems.append("HOLDOUT_ROOT_MISSING")
    if is_author:
        problems += [f"{k.upper()}_READABLE" for k, v in paths.items() if v == "READABLE"]
        if not sealed_inside:
            problems.append("SEALED_DATA_OUTSIDE_HOLDOUT")
    return {"user": who, "is_author": is_author, "holdout_root_exists": exists, "paths": paths,
            "sealed_data_inside_holdout": sealed_inside, "problems": problems,
            "result": "PASS" if is_author and not problems else ("FAIL" if is_author else "NOT_APPLICABLE"),
            "note": "要以作者身份运行才算验收；Claude 的 Read 工具那一条另行实测" if not is_author else
                    "Claude 的 Read 工具那一条由 .claude/settings.json 的拒绝规则负责，另行实测"}
