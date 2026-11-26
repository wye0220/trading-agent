"""给模型的上下文、输出校验、解释请求队列。

模型只解释确定性引擎已经给出的结果。这里的任何失败都不影响信号、订单、成交或盈亏。
本模块不调用模型；调用在阶段 4、10 用 claude -p 完成。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .config import KINDS, EngineConfig

CHARS_PER_TOKEN = 2          # 保守估算：中文和 JSON 标点较多，按每 token 2 个字符算
TRIM_ORDER = ("evidence.filters", "evidence.target", "evidence.signal_bar_quality", "structure.enabled_setups",
              "evidence.reference_levels", "reference_levels", "paper_position", "evidence", "structure")
RAW_BAR_KEYS = ("bars", "ohlc_series", "candles", "htf_1h", "swings")


class ContextTooLarge(ValueError):
    pass


@lru_cache(maxsize=None)
def _validator(schema_dir: str, kind: str) -> Draft202012Validator:
    if kind not in KINDS:
        raise KeyError(f"未知的输出类型：{kind}")
    schema = json.loads((Path(schema_dir) / f"{kind}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def schema_errors(project_root: Path, kind: str, obj: Any) -> list[str]:
    v = _validator(str(Path(project_root) / "schemas"), kind)
    return [f"{'/'.join(str(p) for p in e.path) or '(根)'}：{e.message}" for e in sorted(v.iter_errors(obj), key=str)]


def validate_output(con: sqlite3.Connection | None, project_root: Path, kind: str, obj: Any, *,
                    scope: str | None = None, ref: str | None = None) -> bool:
    """校验模型输出。不合格的输出应被调用方丢弃；这里记一条 WARN 告警并返回 False。"""
    errs = schema_errors(project_root, kind, obj)
    if errs and con is not None:
        with con:
            con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, created_at_utc)"
                        " VALUES ('WARN','LLM_SCHEMA_INVALID',?,?,?,?)",
                        (scope, f"{kind} 输出未通过 schema 校验，已丢弃",
                         json.dumps({"ref": ref, "errors": errs[:10]}, ensure_ascii=False), int(time.time())))
    return not errs


def estimate_tokens(obj: Any) -> int:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return -(-len(text) // CHARS_PER_TOKEN)


def _drop(d: dict, path: str) -> bool:
    node = d
    parts = path.split(".")
    for p in parts[:-1]:
        node = node.get(p)
        if not isinstance(node, dict):
            return False
    return node.pop(parts[-1], None) is not None


def _assert_no_raw_bars(obj: Any, path: str = ""):
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in RAW_BAR_KEYS and isinstance(v, (list, tuple)) and len(v) > 1:
                raise ValueError(f"上下文里不能有原始 K 线序列：{path}{k}")
            _assert_no_raw_bars(v, f"{path}{k}.")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _assert_no_raw_bars(v, f"{path}{i}.")


def build_scan_context(engine_cfg: EngineConfig, *, scope: str, as_of_utc: int, event_id: str, snapshot_id: str | None,
                       signal_status: str, risk_status: str, paper_action: str, order_intent: Mapping | None,
                       decision_metadata: Mapping, paper_position: Mapping | None = None,
                       version_tuple: Mapping | None = None, kind: str = "ScanExplanation") -> dict:
    """为一次扫描构建给模型的上下文：只有结构化事件和决策元数据，没有原始 K 线序列。

    超出 token 预算时按 TRIM_ORDER 依次移除次要内容，并在 trimmed 里列出移除了什么；
    只剩核心字段仍超预算时抛 ContextTooLarge。
    """
    meta = json.loads(json.dumps(dict(decision_metadata), ensure_ascii=False, default=str))
    ctx = {
        "kind": kind, "scope": scope, "as_of_utc": as_of_utc, "event_id": event_id, "snapshot_id": snapshot_id,
        "signal_status": signal_status, "risk_status": risk_status, "paper_action": paper_action,
        "order_intent": None if order_intent is None else {k: v for k, v in dict(order_intent).items() if k != "evidence"},
        "decision": {k: meta.get(k) for k in ("setup", "setup_id", "label", "side", "skip_reason", "valid_until_utc",
                                              "entry_trigger_ticks", "entry_limit_ticks", "stop_ticks", "target",
                                              "target_rule", "next_confirmation", "signal_bar_end_utc")},
        "structure": meta.get("structure") or {}, "evidence": meta.get("evidence") or {},
        "paper_position": None if paper_position is None else dict(paper_position),
        "version_tuple": dict(version_tuple or {}), "trimmed": [],
    }
    _assert_no_raw_bars(ctx)
    budget = engine_cfg.token_budgets[kind]
    for path in TRIM_ORDER:
        if estimate_tokens(ctx) <= budget:
            break
        if _drop(ctx, path):
            ctx["trimmed"].append(path)
    used = estimate_tokens(ctx)
    if used > budget:
        raise ContextTooLarge(f"{kind} 的上下文裁到只剩核心字段仍有约 {used} token，超过预算 {budget}")
    return ctx


# ---------------------------------------------------------------- 解释请求队列
def enqueue_explanation(con: sqlite3.Connection, kind: str, context: Mapping, *, scope: str | None = None,
                        snapshot_id: str | None = None, event_id: str | None = None) -> str:
    """把一个解释请求连同它依据的快照放进队列。同一 (kind, event_id, snapshot_id) 重复入队是空操作。"""
    if kind not in KINDS:
        raise KeyError(f"未知的输出类型：{kind}")
    rid = "exp_" + hashlib.sha256(f"{kind}|{event_id}|{snapshot_id}".encode()).hexdigest()[:20]
    now = int(time.time())
    with con:
        con.execute("INSERT OR IGNORE INTO explanation_requests (request_id, kind, scope, snapshot_id, event_id,"
                    " context_json, status, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,'PENDING',?,?)",
                    (rid, kind, scope, snapshot_id, event_id, json.dumps(dict(context), ensure_ascii=False), now, now))
    return rid


def pending_explanations(con: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM explanation_requests WHERE status='PENDING' ORDER BY created_at_utc, request_id"
                       " LIMIT ?", (limit,)).fetchall()


def complete_explanation(con: sqlite3.Connection, request_id: str, result: Mapping, *,
                         model_id: str | None = None) -> None:
    """标记完成。model_id 是实际生成结果的模型；不知道时留 NULL，不拿配置里的别名充数。"""
    with con:
        con.execute("UPDATE explanation_requests SET status='DONE', result_json=?, model_id=?, version=version+1,"
                    " updated_at_utc=? WHERE request_id=? AND status='PENDING'",
                    (json.dumps(dict(result), ensure_ascii=False), model_id, int(time.time()), request_id))


def fail_explanation(con: sqlite3.Connection, engine_cfg: EngineConfig, request_id: str, error: str) -> str:
    """登记一次失败。未到上限时留在队列里（快照不变）；达到上限时转入死信并告警。返回新的状态。"""
    now = int(time.time())
    with con:
        r = con.execute("SELECT * FROM explanation_requests WHERE request_id=?", (request_id,)).fetchone()
        if r is None or r["status"] != "PENDING":
            return "MISSING" if r is None else r["status"]
        attempts = r["attempts"] + 1
        dead = attempts >= engine_cfg.max_explanation_retries
        con.execute("UPDATE explanation_requests SET attempts=?, last_error=?, status=?, version=version+1,"
                    " updated_at_utc=? WHERE request_id=?",
                    (attempts, error[:500], "DEAD" if dead else "PENDING", now, request_id))
        if dead:
            con.execute("INSERT OR REPLACE INTO dead_letter_queue VALUES (?,?,?,?,?,?,?,?)",
                        (request_id, "explanation:" + r["kind"],
                         json.dumps({"snapshot_id": r["snapshot_id"], "event_id": r["event_id"],
                                     "context": json.loads(r["context_json"])}, ensure_ascii=False),
                         error[:500], attempts, "DEAD", r["created_at_utc"], now))
            con.execute("INSERT INTO alerts (level, kind, scope, message, detail_json, created_at_utc)"
                        " VALUES ('WARN','EXPLANATION_DEAD_LETTER',?,?,?,?)",
                        (r["scope"], f"{r['kind']} 解释请求连续失败 {attempts} 次，转入死信队列",
                         json.dumps({"request_id": request_id, "error": error[:200]}, ensure_ascii=False), now))
        return "DEAD" if dead else "PENDING"
