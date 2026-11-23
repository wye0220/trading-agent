"""Memory 条目的校验和模板句子。先过 JSON schema（类型、长度、字符集），再过登记表（语义）。"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from jsonschema import Draft202012Validator

from ..config import Settings
from .registry import KINDS, Registry, forbidden

TERM = re.compile(r"^([a-z][a-z0-9_]*)(>=|<=|=|>|<)([a-z0-9_]+)$")
CONTRACT = re.compile(r"^([a-z]{1,4})([fghjkmnquvxz])(\d{4})$")
OPS = {"=": "为", ">=": "不小于", "<=": "不大于", ">": "大于", "<": "小于"}
IDENTITY = ("subject", "condition", "metric")


class Invalid(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=None)
def _validator(schema_dir: str, name: str) -> Draft202012Validator:
    schema = json.loads((Path(schema_dir) / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_errors(project_root: Path, name: str, obj) -> list[str]:
    v = _validator(str(Path(project_root) / "schemas"), name)
    return [f"{'/'.join(str(p) for p in e.path) or '(根)'}：{e.message}" for e in sorted(v.iter_errors(obj), key=str)]


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def item_id(body: Mapping) -> str:
    s = body["statement"]
    key = "|".join([body["kind"], body["scope"], *(str(s[k]) for k in IDENTITY)])
    return "mi_" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _roots(settings: Settings) -> set[str]:
    return {r.lower() for r in settings.instruments}


def parse_condition(cond: str | None, reg: Registry, settings: Settings) -> list[tuple[str, str, str]]:
    if cond is None:
        return []
    out, seen = [], set()
    for term in cond.split("."):
        m = TERM.match(term)
        if not m:
            raise Invalid("CONDITION_NOT_REGISTERED", f"condition 的写法应为 键=值，用 . 连接：{term!r}")
        key, op, val = m.groups()
        why = forbidden(key) or forbidden(val)
        if why:
            raise Invalid(why, f"condition 不能引用风险上限或实时状态：{term}")
        spec = reg.condition_keys.get(key)
        if spec is None or key in seen:
            raise Invalid("CONDITION_NOT_REGISTERED", f"condition 的键未登记或重复：{key}")
        seen.add(key)
        if "values" in spec:
            if op != "=" or val not in spec["values"]:
                raise Invalid("CONDITION_NOT_REGISTERED", f"{key} 的取值未登记：{val}")
        elif "int" in spec:
            if not val.isdigit() or not spec["int"][0] <= int(val) <= spec["int"][1]:
                raise Invalid("CONDITION_NOT_REGISTERED", f"{key} 应为 {spec['int'][0]} 到 {spec['int'][1]} 的整数：{val}")
        else:
            c = CONTRACT.match(val)
            if op != "=" or not c or c.group(1) not in _roots(settings):
                raise Invalid("CONDITION_NOT_REGISTERED", f"{key} 不是已登记品种的合约：{val}")
        out.append((key, op, val))
    return out


def _check_value(value, unit, spec: Mapping, metric: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Invalid("VALUE_OUT_OF_RANGE", f"{metric} 的值应为有限的数：{value!r}")
    if unit != spec["unit"]:
        raise Invalid("UNIT_MISMATCH", f"{metric} 的单位应为 {spec['unit']!r}，不是 {unit!r}")
    if "min" in spec:
        if not isinstance(value, int) or not spec["min"] <= value <= spec["max"]:
            raise Invalid("VALUE_OUT_OF_RANGE", f"{metric} 的值应为 {spec['min']} 到 {spec['max']} 的整数：{value!r}")
    elif unit == "ratio":
        if not 0 <= value <= 1:
            raise Invalid("VALUE_OUT_OF_RANGE", f"{metric} 是比例，应在 0 到 1 之间：{value!r}")
    elif unit == "count":
        if not isinstance(value, int) or value < 0:
            raise Invalid("VALUE_OUT_OF_RANGE", f"{metric} 应为非负整数：{value!r}")
    elif unit == "yyyymmdd":
        try:
            if not isinstance(value, int):
                raise ValueError
            date(value // 10000, value // 100 % 100, value % 100)
        except ValueError:
            raise Invalid("VALUE_OUT_OF_RANGE", f"{metric} 应为 yyyymmdd 形式的日期：{value!r}") from None


def validate(settings: Settings, reg: Registry, body, *, now_utc: int | None = None) -> dict:
    """校验候选主体（kind、scope、statement、source_event_ids、valid_until）。通过则返回规范化的副本。"""
    errs = schema_errors(settings.project_root, "MemoryCandidate", body)
    if errs:
        raise Invalid("SCHEMA_INVALID", "；".join(errs[:3]))
    kind, scope, s = body["kind"], body["scope"], body["statement"]
    assert kind in KINDS
    if scope != "global":
        fam, root, _, tf = scope.split(":")
        if root not in settings.instruments or tf != "5m":
            raise Invalid("SCOPE_NOT_REGISTERED", f"scope 未登记：{scope}")
    for name in (s["subject"], s["metric"]):
        why = forbidden(name)
        if why:
            raise Invalid(why, f"{name} 只能从账本和风控配置读，不能写进 Memory")
    spec = reg.metric(kind, s["metric"])
    if spec is None:
        raise Invalid("METRIC_NOT_REGISTERED", f"{kind} 没有登记指标 {s['metric']}")
    if s["source"] not in reg.sources[kind]:
        raise Invalid("SOURCE_NOT_REGISTERED", f"{kind} 的来源只能是 {'、'.join(reg.sources[kind])}")
    conds = parse_condition(s["condition"], reg, settings)
    _check_value(s["value"], s["unit"], spec, s["metric"])
    if s["period"] is not None:
        try:
            a, b = (date.fromisoformat(x) for x in s["period"].split(".."))
            if a > b:
                raise ValueError
        except ValueError:
            raise Invalid("SCHEMA_INVALID", f"period 不是合法的日期区间：{s['period']}") from None
    until = None
    if body["valid_until"] is not None:
        try:
            until = parse_iso(body["valid_until"])
        except ValueError:
            raise Invalid("SCHEMA_INVALID", f"valid_until 不是合法的时刻：{body['valid_until']}") from None
        if now_utc is not None and until <= now_utc:
            raise Invalid("ALREADY_EXPIRED", "valid_until 已经过了")
    if kind == "user_preference":
        if s["subject"] != "user_preference" or conds or s["sample_n"] is not None or s["period"] is not None:
            raise Invalid("SUBJECT_NOT_REGISTERED", "偏好的 subject 是 user_preference，不带条件、样本数和区间")
    elif kind == "market_fact":
        head, _, root = s["subject"].partition(".")
        if head != "instrument" or root not in _roots(settings):
            raise Invalid("SUBJECT_NOT_REGISTERED", f"市场事实的 subject 应为 instrument.<已登记的根代码>：{s['subject']}")
        if until is None:
            raise Invalid("VALID_UNTIL_REQUIRED", "市场事实必须有有效期")
        if s["sample_n"] is not None:
            raise Invalid("SCHEMA_INVALID", "市场事实不带样本数")
    else:
        if s["subject"] != "trader":
            raise Invalid("SUBJECT_NOT_REGISTERED", f"执行模式的 subject 是 trader：{s['subject']}")
        if (s["sample_n"] or 0) < reg.discipline_min_sample_n or s["period"] is None or not body["source_event_ids"]:
            raise Invalid("EVIDENCE_INSUFFICIENT",
                          f"执行模式需要至少 {reg.discipline_min_sample_n} 个样本、统计区间和证据 id")
    return json.loads(canon(body))


def _num(v) -> str:
    return str(v) if isinstance(v, int) else f"{v:.4f}".rstrip("0").rstrip(".")


def _value_text(value, unit) -> str:
    if unit == "ratio":
        return f"{_num(round(value * 100, 2))}%"
    if unit == "yyyymmdd":
        return f"{value // 10000:04d}-{value // 100 % 100:02d}-{value % 100:02d}"
    return _num(value) + {"s": " 秒", "r": " R", "count": " 天", None: ""}[unit]


def condition_text(cond: str | None, reg: Registry, settings: Settings) -> str:
    return "、".join(f"{reg.condition_keys[k]['label']}{OPS[op]} {v}" for k, op, v in parse_condition(cond, reg, settings))


def sentence(settings: Settings, reg: Registry, item: Mapping) -> str:
    """模板句子：只由条目字段和登记表里的标签拼出。"""
    s, kind = item["statement"], item["kind"]
    label = reg.metric(kind, s["metric"])["label"]
    val = _value_text(s["value"], s["unit"])
    where = "全局" if item["scope"] == "global" else item["scope"]
    tail = []
    if s["sample_n"] is not None:
        tail.append(f"样本 {s['sample_n']}")
    if s["period"] is not None:
        tail.append(f"区间 {s['period']}")
    tail.append(f"来源 {s['source']}")
    if item.get("valid_until"):
        tail.append(f"有效至 {item['valid_until']}")
    cond = condition_text(s["condition"], reg, settings)
    if kind == "user_preference":
        head = f"偏好（{where}）：{label}（{s['metric']}）= {val}"
    elif kind == "market_fact":
        head = f"市场事实（{where}）：{s['subject'].split('.')[1].upper()}" + (f"（{cond}）" if cond else "") + f" 的{label}是 {val}"
    else:
        head = f"执行模式（{where}）：" + (f"{cond}时，" if cond else "") + f"{label}（{s['metric']}）为 {val}"
    ident = f" [{item['item_id']}]" if item.get("item_id") else ""
    return f"{head}（{'，'.join(tail)}）{ident}"
