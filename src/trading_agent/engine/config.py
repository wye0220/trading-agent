"""引擎与风控配置的加载。金额类字段留空表示"未配置"，不猜默认值。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

import yaml

from ..config import ConfigError

KINDS = ("ScanExplanation", "PretradeCheck", "SessionReview", "Candidate", "MemoryCandidate", "ReleaseReview")


@dataclass(frozen=True)
class AccountLimits:
    approved: bool
    budget_per_trade_usd: Decimal | None
    daily_loss_limit_usd: Decimal | None
    max_contracts: int | None
    max_stops_per_day: int
    snapshot_max_age_seconds: int

    @property
    def configured(self) -> bool:
        return None not in (self.budget_per_trade_usd, self.daily_loss_limit_usd, self.max_contracts)


@dataclass(frozen=True)
class RiskConfig:
    fee_mode: str                       # per_side | round_trip
    fee_amount_usd: Decimal | None
    entry_slippage_ticks: int
    stop_slippage_ticks: int
    slippage_budget_ticks: int
    paper_quantity: int
    research_risk_unit_usd: Decimal | None
    account: AccountLimits
    version: str                        # 配置内容的摘要

    @property
    def fees_configured(self) -> bool:
        return self.fee_amount_usd is not None

    @property
    def round_trip_fee_usd(self) -> Decimal:
        """每手往返费用。未配置时为 0（调用方要带 FEES_NOT_CONFIGURED 标记）。"""
        if self.fee_amount_usd is None:
            return Decimal(0)
        return self.fee_amount_usd * (2 if self.fee_mode == "per_side" else 1)


@dataclass(frozen=True)
class EngineConfig:
    flatten_minutes_before_close: int
    replay_activation_delay_seconds: int
    max_explanation_retries: int
    token_budgets: Mapping[str, int]


def _money(v, path) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise ConfigError(f"{path} 应为非负金额：{v!r}") from None
    if isinstance(v, bool) or d < 0 or not d.is_finite():
        raise ConfigError(f"{path} 应为非负金额：{v!r}")
    return d


def _nat(v, path, lo=0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ConfigError(f"{path} 应为 ≥{lo} 的整数：{v!r}")
    return v


def parse_risk(raw: Mapping) -> RiskConfig:
    try:
        fees, slip, paper, acct = raw["fees"], raw["slippage"], raw["paper"], raw["account"]
        mode = fees["mode"]
        if mode not in ("per_side", "round_trip"):
            raise ConfigError(f"fees.mode 只能是 per_side 或 round_trip：{mode!r}")
        if not isinstance(acct["approved"], bool):
            raise ConfigError("account.approved 应为布尔值")
        limits = AccountLimits(
            approved=acct["approved"],
            budget_per_trade_usd=_money(acct.get("budget_per_trade_usd"), "account.budget_per_trade_usd"),
            daily_loss_limit_usd=_money(acct.get("daily_loss_limit_usd"), "account.daily_loss_limit_usd"),
            max_contracts=None if acct.get("max_contracts") is None else _nat(acct["max_contracts"], "account.max_contracts", 1),
            max_stops_per_day=_nat(acct.get("max_stops_per_day", 2), "account.max_stops_per_day", 1),
            snapshot_max_age_seconds=_nat(acct.get("snapshot_max_age_seconds", 120), "account.snapshot_max_age_seconds", 1))
        ru = _money(paper.get("research_risk_unit_usd"), "paper.research_risk_unit_usd")
        if ru is not None and ru == 0:
            raise ConfigError("paper.research_risk_unit_usd 必须为正")
        version = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:12]
        return RiskConfig(
            fee_mode=mode, fee_amount_usd=_money(fees.get("amount_usd"), "fees.amount_usd"),
            entry_slippage_ticks=_nat(slip["entry_ticks"], "slippage.entry_ticks"),
            stop_slippage_ticks=_nat(slip["stop_ticks"], "slippage.stop_ticks"),
            slippage_budget_ticks=_nat(slip["budget_ticks"], "slippage.budget_ticks"),
            paper_quantity=_nat(paper["quantity"], "paper.quantity", 1), research_risk_unit_usd=ru,
            account=limits, version="risk-" + version)
    except (KeyError, TypeError) as e:
        raise ConfigError(f"风控配置缺少或写错了字段：{e}") from None


def parse_engine(raw: Mapping) -> EngineConfig:
    try:
        budgets = {k: _nat(v, f"token_budgets.{k}", 1) for k, v in raw["token_budgets"].items()}
        missing = [k for k in KINDS if k not in budgets]
        if missing:
            raise ConfigError(f"token_budgets 缺少：{missing}")
        return EngineConfig(
            flatten_minutes_before_close=_nat(raw["flatten_minutes_before_close"], "flatten_minutes_before_close"),
            replay_activation_delay_seconds=_nat(raw["replay_activation_delay_seconds"], "replay_activation_delay_seconds"),
            max_explanation_retries=_nat(raw["explanations"]["max_retries"], "explanations.max_retries", 1),
            token_budgets=budgets)
    except (KeyError, TypeError) as e:
        raise ConfigError(f"引擎配置缺少或写错了字段：{e}") from None


def _yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_risk(project_root: Path) -> RiskConfig:
    return parse_risk(_yaml(Path(project_root) / "config" / "risk.yaml"))


def load_engine(project_root: Path) -> EngineConfig:
    return parse_engine(_yaml(Path(project_root) / "config" / "engine.yaml"))
