"""策略配置变体：把一组"键路径 → 新值"的结构化差异加到基线配置上。

通用工具，不认识任何具体策略。键路径用点分隔；路过由带 name 的条目组成的列表时，用 name 取条目
（例如 setups.<形态名>.exempt）。只有命中白名单的键才允许改；白名单由各策略自己登记。
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping


class VariantError(ValueError):
    pass


def allowed(path: str, patterns: Iterable[str]) -> bool:
    """逐段匹配，* 只匹配一段。"""
    parts = path.split(".")
    for pat in patterns:
        pp = pat.split(".")
        if len(pp) == len(parts) and all(a == "*" or a == b for a, b in zip(pp, parts)):
            return True
    return False


def _child(node, key: str, path: str):
    if isinstance(node, Mapping):
        if key not in node:
            raise VariantError(f"配置里没有这个键：{path}")
        return node[key]
    if isinstance(node, list):
        for item in node:
            if isinstance(item, Mapping) and item.get("name") == key:
                return item
        raise VariantError(f"配置里没有这个条目：{path}")
    raise VariantError(f"键路径走不通：{path}")


def apply_diff(raw: Mapping[str, Any], diff: Mapping[str, Any], patterns: Iterable[str]) -> dict:
    """返回加了差异的配置副本。键不在白名单、路径不存在、或差异为空都报错。"""
    if not isinstance(diff, Mapping) or not diff:
        raise VariantError("差异不能为空")
    patterns = tuple(patterns)
    out = copy.deepcopy(dict(raw))
    for path, value in diff.items():
        if not isinstance(path, str) or not allowed(path, patterns):
            raise VariantError(f"不在可改键的白名单内：{path}")
        parts = path.split(".")
        node = out
        for i, key in enumerate(parts[:-1]):
            node = _child(node, key, ".".join(parts[:i + 1]))
        _child(node, parts[-1], path)                 # 目标键必须已存在
        if not isinstance(node, dict):
            raise VariantError(f"键路径走不通：{path}")
        node[parts[-1]] = copy.deepcopy(value)
    return out
