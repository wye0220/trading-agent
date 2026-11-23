"""baseline 命令的总编排：登记切分 → 冻结基线 → 基线报告 → 预登记扰动 → 功效报告。"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..config import Scope, Settings, load_settings, parse_scope
from ..engine.config import load_engine, load_risk
from . import baseline as bl
from . import metrics, power, report, splits
from .config import ResearchConfig, load_research, validate_perturbations


def _variant_worker(args) -> dict:
    """子进程：自己开连接，跑一个扰动。只在用默认注册表时使用。"""
    from .. import db
    from ..strategies.registry import default_registry
    project_root, data_root, release_db, cutoff, scope_text, baseline_id, pert_id, allow = args
    settings = load_settings(project_root, data_root=data_root, release_db=release_db, dev_visible_through=cutoff)
    scope = parse_scope(scope_text, settings)
    cfg = load_research(settings.project_root)
    con = db.open_runtime(settings)
    try:
        base = bl.get(con, baseline_id)
        reg = splits.build(settings, con, scope.instrument_root, cfg)
        pert = next(p for p in cfg.perturbations_for(scope.strategy_family) if p.id == pert_id)
        return bl.run_variant(settings, con, scope, default_registry(), base, reg, pert, allow_real_data=allow)
    finally:
        con.close()


def run_baseline(settings: Settings, con: sqlite3.Connection, scope: Scope, registry, *,
                 research_cfg: ResearchConfig | None = None, risk_cfg=None, engine_cfg=None,
                 allow_real_data: bool = False, skip_power: bool = False, jobs: int = 1,
                 out_dir: Path | None = None, log=lambda msg: None) -> dict:
    research_cfg = research_cfg or load_research(settings.project_root)
    risk_cfg = risk_cfg or load_risk(settings.project_root)
    engine_cfg = engine_cfg or load_engine(settings.project_root)
    family = scope.strategy_family
    strategy = registry.get(family)
    perts = research_cfg.perturbations_for(family)
    if not skip_power:
        validate_perturbations(research_cfg, registry, family)      # 先验证全部扰动，再开始耗时的回放

    base = bl.freeze(settings, con, scope, strategy, research_cfg, risk_cfg=risk_cfg, engine_cfg=engine_cfg,
                     allow_real_data=allow_real_data, log=log)
    reg = base["registry"]
    paths = report.paths(settings, scope, out_dir)
    data = report.collect(settings, con, scope, base, research_cfg, risk_cfg)
    report.write_baseline(data, paths)
    log(f"基线报告：{paths['baseline_md']}")
    out = {"ok": base["status"] == bl.FROZEN, "baseline_id": base["baseline_id"], "status": base["status"],
           "scope": str(scope), "flags": base["flags"], "registry_id": reg["registry_id"], "splits": reg["summary"],
           "reused": base.get("reused", False), "strategy_validity": data["strategy_validity"],
           "total": {k: (str(v) if k == "net_usd" else v) for k, v in data["segments"]["total"]["fixed"].items()
                     if k in ("days", "trades", "net_usd", "net_r_per_day", "win_rate", "profit_factor")},
           "reports": {"baseline": str(paths["baseline_md"]), "baseline_json": str(paths["baseline_json"]),
                       "trades_csv": str(paths["trades_csv"])}}
    if base["status"] != bl.FROZEN or skip_power:
        out["power"] = None
        return out

    dev_days = data["measured_days"]["development"]
    kw = dict(risk_cfg=risk_cfg, engine_cfg=engine_cfg, allow_real_data=allow_real_data)
    if jobs > 1 and len(perts) > 1:
        args = [(str(settings.project_root), str(settings.data_root), str(settings.release_db),
                 settings.dev_visible_through, str(scope), base["baseline_id"], p.id, allow_real_data) for p in perts]
        log(f"并行回放 {len(perts)} 个扰动（{jobs} 个进程）")
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            list(ex.map(_variant_worker, args))
    variants = []
    for p in perts:
        log(f"扰动 {p.id}（{p.track}）")
        res = bl.run_variant(settings, con, scope, registry, base, reg, p, **kw)      # 已跑过的直接复用
        trades = metrics.load_trades(settings, con, scope, res["run_id"], risk_cfg)
        variants.append({"id": p.id, "track": p.track, "note": p.note, "run_id": res["run_id"],
                         "series": metrics.daily_series(trades, dev_days, risk_cfg.research_risk_unit_usd)})
    base_series = metrics.daily_series(data["trades"], dev_days, risk_cfg.research_risk_unit_usd)
    inst = settings.instrument(scope.instrument_root)
    unit = risk_cfg.research_risk_unit_usd
    stress = float(2 * research_cfg.power.stress_extra_ticks_per_side * inst.tick_value_usd / unit) if unit else 0.0
    pw = power.analyze(base_series, variants, research_cfg.power, stress_r_per_trade=stress,
                       available_days=reg["summary"]["capsule_valid_days"], fixture=reg["fixture"])
    pw["baseline_id"] = base["baseline_id"]
    for v in pw["variants"]:
        v.pop("series", None)
    report.write_power(str(scope), pw, research_cfg.power, paths)
    log(f"功效报告：{paths['power_md']}")
    out["power"] = {t: {"eligibility": v["eligibility"], "joint_pass_rate": v["joint_pass_rate"]}
                    for t, v in pw["tracks"].items()}
    out["reports"]["power"] = str(paths["power_md"])
    return out
