"""Memory 的加载与 Context 构建：按 scope 和版本取有效条目，按任务筛选，核对输出里的引用。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter

from ..config import Settings
from . import items as I
from . import store
from .registry import Registry, load_registry

ITEM_REF = re.compile(r"mi_[0-9a-f]{16}")
ORDER = {"user_preference": 0, "market_fact": 1, "discipline_pattern": 2}


def load(settings: Settings, con: sqlite3.Connection, scope, *, memory_version: str | None = None,
         now_utc: int | None = None, reg: Registry | None = None) -> dict:
    """指定（缺省为头指针的）memory_version 在该 scope 下的有效条目。scope 为空时只取 global。"""
    reg = reg or load_registry(settings.project_root)
    now = int(time.time()) if now_utc is None else now_utc
    if memory_version is None:
        h = store.head(con)
        memory_version = h["memory_version"] if h else None
    scope_text = str(scope) if scope else None
    out, excluded = [], Counter()
    for iid, ver in store.members(con, memory_version):
        r = con.execute("SELECT * FROM memory_items WHERE item_id=? AND version=?", (iid, ver)).fetchone()
        if r is None:
            excluded["MISSING"] += 1
            continue
        if r["scope"] not in ("global", scope_text):
            continue
        if r["valid_until_utc"] is not None and r["valid_until_utc"] <= now:
            excluded["EXPIRED"] += 1
            continue
        body = {"kind": r["kind"], "scope": r["scope"], "statement": json.loads(r["statement_json"]),
                "source_event_ids": json.loads(r["source_event_ids_json"]),
                "valid_until": I.iso(r["valid_until_utc"]) if r["valid_until_utc"] is not None else None}
        try:
            I.validate(settings, reg, body)
            if not r["confirmed_by"] or I.item_id(body) != iid:
                raise I.Invalid("SCHEMA_INVALID", "条目没有确认者或标识与内容不符")
        except I.Invalid:
            excluded["INVALID"] += 1
            continue
        item = {"item_id": iid, "version": ver, **body, "confirmed_by": r["confirmed_by"],
                "created_at": I.iso(r["created_at_utc"])}
        item["sentence"] = I.sentence(settings, reg, item)
        out.append(item)
    out.sort(key=lambda i: (ORDER[i["kind"]], i["scope"] != "global", i["item_id"]))
    digest = hashlib.sha256(I.canon([[i["item_id"], i["version"]] for i in out]).encode()).hexdigest()[:16]
    return {"memory_version": memory_version, "scope": scope_text or "global", "items": out,
            "excluded": dict(excluded), "scope_digest": digest, "registry_version": reg.version}


def preferences(settings: Settings, con: sqlite3.Connection, scope=None, *, now_utc: int | None = None,
                reg: Registry | None = None) -> dict:
    """已生效的偏好，键 → 值。四维 scope 的偏好盖过 global 的。"""
    out = {}
    for i in load(settings, con, scope, now_utc=now_utc, reg=reg)["items"]:
        if i["kind"] == "user_preference":
            out[i["statement"]["metric"]] = i["statement"]["value"]     # 排序保证具体 scope 的在后
    return out


def build(settings: Settings, con: sqlite3.Connection, scope, task: str, *, memory_version: str | None = None,
          now_utc: int | None = None, reg: Registry | None = None) -> dict:
    """给一类任务注入的 Memory：只含登记为相关的条目，不超过上限。输出要引用 item_id。"""
    reg = reg or load_registry(settings.project_root)
    if task not in reg.relevance:
        raise KeyError(f"未登记的任务类型：{task}")
    rel = reg.relevance[task]
    loaded = load(settings, con, scope, memory_version=memory_version, now_utc=now_utc, reg=reg)
    picked = []
    for i in loaded["items"]:
        want = rel.get(i["kind"])
        if want is True or (isinstance(want, tuple) and i["statement"]["metric"] in want):
            picked.append(i)
    dropped = max(0, len(picked) - reg.max_items_per_context)
    picked = picked[:reg.max_items_per_context]
    return {"memory_version": loaded["memory_version"], "scope": loaded["scope"], "task": task,
            "items": [{"item_id": i["item_id"], "kind": i["kind"], "sentence": i["sentence"]} for i in picked],
            "citable_item_ids": [i["item_id"] for i in picked], "dropped_over_limit": dropped}


def check_citations(output, ctx: dict) -> list[str]:
    """输出里出现的 item_id 必须是本次注入的。返回不该出现的标识（空 = 通过）。"""
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    return sorted(set(ITEM_REF.findall(text)) - set(ctx["citable_item_ids"]))
