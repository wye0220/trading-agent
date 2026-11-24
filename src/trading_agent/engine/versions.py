"""版本元组的加载与绑定。每一步开始时加载一次；订单和仓位记下自己生成时的元组。"""
from __future__ import annotations

import sqlite3

from ..config import Scope
from .config import RiskConfig
from .fills import ENGINE_VERSION

UNRELEASED = "UNRELEASED"
FIELDS = ("strategy_family", "strategy_version", "risk_version", "prompt_version", "memory_version",
          "engine_version", "model_id")


def current(scope: Scope, strategy, risk: RiskConfig, release_con: sqlite3.Connection | None = None,
            *, prompt_version: str | None = None, memory_version: str | None = None,
            model_id: str | None = None) -> dict:
    """该 scope 当前的版本元组。发布库里有 active 指针时带上它的 release_id 和 manifest_hash，否则标为未发布。"""
    out = {"strategy_family": strategy.strategy_family, "strategy_version": strategy.version,
           "risk_version": risk.version, "prompt_version": prompt_version, "memory_version": memory_version,
           "engine_version": ENGINE_VERSION, "model_id": model_id, "release_id": UNRELEASED, "manifest_hash": None}
    if release_con is not None:
        r = release_con.execute(
            "SELECT release_id, manifest_hash FROM active_pointers WHERE strategy_family=? AND instrument_root=?"
            " AND session_type=? AND timeframe=?",
            (scope.strategy_family, scope.instrument_root, scope.session_type, scope.timeframe)).fetchone()
        if r is not None:
            out["release_id"], out["manifest_hash"] = r[0], r[1]
    return out
