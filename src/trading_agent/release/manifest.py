"""发布清单。同样的状态得到同样的清单和 manifest_hash：清单里不放时间戳和运行耗时。

两类：
- CANDIDATE：影子通过的候选，previous_release 是当前 active；配置差异 = 上一版本的差异 + 候选差异。
- INITIAL_PAPER_BASELINE：scope 没有 active 时建档；绑定基线，标 UNVALIDATED_BASELINE，附五项验收。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from ..config import Scope, Settings
from ..engine import versions
from ..engine.config import RiskConfig
from ..evolve import candidates as cands
from ..evolve.queue import ENGINE_FLAGS, base_release_of
from ..manual.common import paper_run_id
from ..memory import store as mem
from ..research import baseline as bl
from ..research import metrics
from . import pointer
from .config import ReleaseConfig
from .guard import ReleaseError, release_digest

UNVALIDATED = "UNVALIDATED_BASELINE"


def sha(obj) -> str:
    return hashlib.sha256(pointer.canon(obj).encode()).hexdigest()


def manifest_hash(m: dict) -> str:
    return sha(m)


def release_id_of(core: dict) -> str:
    return "rel_" + sha(core)[:16]


def strategy_for(registry, scope: Scope, config_diff: dict, label: str):
    """一个版本的策略：正式配置加累积差异。没有差异就是正式配置本身。"""
    if not config_diff:
        return registry.get(scope.strategy_family)
    return registry.variant(scope.strategy_family, dict(config_diff), label)


def frozen_baseline(con: sqlite3.Connection, scope_text: str, baseline_id: str | None = None) -> dict:
    if baseline_id:
        b = bl.get(con, baseline_id)
        if b is None or b["scope"] != scope_text:
            raise ReleaseError("NO_BASELINE", f"{scope_text} 没有基线 {baseline_id}")
    else:
        r = con.execute("SELECT baseline_id FROM baselines WHERE scope=? AND status='FROZEN' AND run_id IS NOT NULL"
                        " ORDER BY updated_at_utc DESC LIMIT 1", (scope_text,)).fetchone()
        if r is None:
            raise ReleaseError("NO_BASELINE", f"{scope_text} 没有已冻结的基线")
        b = bl.get(con, r[0])
    if b["status"] != "FROZEN" or not b["run_id"]:
        raise ReleaseError("NO_BASELINE", f"基线 {b['baseline_id']} 的状态是 {b['status']}，不能作为发布依据")
    return b


# ---------------------------------------------------------------- 预期区间
def _dev_days(con: sqlite3.Connection, registry_id: str) -> list[str]:
    out = []
    for (dj,) in con.execute("SELECT days_json FROM data_segments WHERE registry_id=? AND kind='DEVELOPMENT'"
                             " ORDER BY seq", (registry_id,)):
        out += [d[0] for d in json.loads(dj) if d[1]]
    return out


def _setups_per_day(con: sqlite3.Connection, run_id: str, days: list[str]) -> float | None:
    if not days:
        return None
    n = con.execute("SELECT COUNT(*) FROM scan_events WHERE run_id=? AND signal_status='SETUP_VALID'"
                    " AND json_extract(decision_json,'$.trading_day') BETWEEN ? AND ?", (run_id, days[0], days[-1])).fetchone()[0]
    return n / len(days)


def _f(x) -> float | None:
    return None if x is None else float(x)


def dev_expectations(settings: Settings, con: sqlite3.Connection, scope: Scope, base: dict, risk_cfg: RiskConfig) -> dict:
    """阶段 5 开发段的基线主运行：首次纸面基线的预期区间来源，也是候选版本的波动参照。"""
    days = _dev_days(con, base["registry_id"])
    trades = metrics.load_trades(settings, con, scope, base["run_id"], risk_cfg)
    inst = settings.instrument(scope.instrument_root)
    s = metrics.summarize(trades, days, tick_value=inst.tick_value_usd, research_unit=risk_cfg.research_risk_unit_usd)
    ser = metrics.daily_series(trades, days, risk_cfg.research_risk_unit_usd)
    net = ser["net"]
    mean = sum(net) / len(net) if net else None
    std = (math.sqrt(sum((x - mean) ** 2 for x in net) / (len(net) - 1)) if len(net) > 1 else None)
    return {"source": "DEV_BASELINE", "baseline_id": base["baseline_id"], "run_id": base["run_id"],
            "valid_days": len(days), "unit": ser["unit"], "net_per_day": mean, "daily_std": std,
            "max_drawdown": metrics.max_drawdown(net) if net else None,
            "trades_per_day": s["trades_per_day"], "setups_per_day": _setups_per_day(con, base["run_id"], days),
            "ambiguous_bar_share": s["ambiguous_bar_share"]}


def rollback_trigger(expected: dict, cfg: ReleaseConfig) -> dict:
    """监控的触发条件：准备清单时算好、冻结进清单。缺输入的条件记为不判定（null）。"""
    m = cfg.monitor
    n = m["rolling_valid_days"]
    mean, std = expected.get("net_per_day"), expected.get("daily_std")
    k = m["frequency_ratio"]
    amb = expected.get("ambiguous_bar_share")
    dd = expected.get("max_drawdown")
    return {
        "unit": expected.get("unit"), "rolling_valid_days": n,
        "rolling_net_per_day_floor": (mean - m["net_r_sigma_multiple"] * std / math.sqrt(n))
        if mean is not None and std is not None else None,
        "drawdown_limit": dd * m["drawdown_multiple"] if dd else None,
        "trades_per_day_band": [expected["trades_per_day"] / k, expected["trades_per_day"] * k]
        if expected.get("trades_per_day") else None,
        "setups_per_day_band": [expected["setups_per_day"] / k, expected["setups_per_day"] * k]
        if expected.get("setups_per_day") else None,
        "ambiguous_bar_share_max": max(m["ambiguous_bar_multiple"] * (amb or 0.0), m["ambiguous_bar_floor"]),
        "ambiguous_min_trades": m["ambiguous_min_trades"],
    }


# ---------------------------------------------------------------- 首次基线的五项验收
def _pytest(settings: Settings, files: tuple[str, ...]) -> dict:
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *files]
    try:
        r = subprocess.run(cmd, cwd=settings.project_root, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as e:
        return {"status": "FAIL", "detail": f"{type(e).__name__}"}
    tail = [x for x in r.stdout.strip().splitlines() if x.strip()][-1:] or [""]
    summary = re.sub(r"\s+in\s+[\d.]+s.*$", "", tail[0]).strip("= ").strip()
    return {"status": "PASS" if r.returncode == 0 else "FAIL", "detail": summary, "files": list(files)}


def ledger_check(con: sqlite3.Connection, run_id: str, *, through_day: str | None = None) -> dict:
    """账本一致：成交的订单有仓位，平掉的仓位有交易，日终没有留仓，没有引擎异常标记。"""
    probs = []
    q = lambda sql, *a: con.execute(sql, (run_id, *a)).fetchall()  # noqa: E731
    for (oid,) in q("SELECT order_id FROM paper_orders o WHERE run_id=? AND status='FILLED' AND NOT EXISTS"
                    " (SELECT 1 FROM paper_positions p WHERE p.order_id=o.order_id)"):
        probs.append(f"FILLED_WITHOUT_POSITION:{oid}")
    for (pid,) in q("SELECT position_id FROM paper_positions p WHERE run_id=? AND status<>'OPEN' AND NOT EXISTS"
                    " (SELECT 1 FROM paper_trades t WHERE t.trade_id=p.position_id)"):
        probs.append(f"CLOSED_WITHOUT_TRADE:{pid}")
    for (pid, d) in q("SELECT position_id, trading_day FROM paper_positions WHERE run_id=? AND status='OPEN'"
                      + (" AND trading_day<?" if through_day else ""), *((through_day,) if through_day else ())):
        probs.append(f"OPEN_AFTER_DAY:{pid}:{d}")
    for (tid, fj) in q("SELECT trade_id, flags_json FROM paper_trades WHERE run_id=?"):
        bad = [f for f in json.loads(fj) if f in ENGINE_FLAGS]
        if bad:
            probs.append(f"ENGINE_FLAG:{tid}:{','.join(bad)}")
    return {"status": "FAIL" if probs else "PASS", "problems": probs[:20], "problem_count": len(probs)}


TIMELY_SECONDS = 300          # 收线后这么久之内处理，才算按时
TIMELY_SHARE = 0.5            # 当天至少这么多的 K 线按时处理，这一天才算纸面运行过


def paper_run_days(con: sqlite3.Connection, scope: Scope) -> list[str]:
    """常驻纸面运行按时处理过的交易日。盘后一次性补跑的日子不算：那时信号都已过期，不是真正的盘中运行。"""
    run_id = paper_run_id(scope)
    per: dict[str, list[int]] = {}
    for day, lag in con.execute(
            "SELECT json_extract(decision_json,'$.trading_day'), as_of_utc - json_extract(decision_json,'$.signal_bar_end_utc')"
            " FROM scan_events WHERE run_id=? AND run_mode='paper'", (run_id,)):
        if day and lag is not None:
            per.setdefault(day, []).append(lag)
    return sorted(d for d, lags in per.items() if sum(x <= TIMELY_SECONDS for x in lags) >= TIMELY_SHARE * len(lags))


def acceptance(settings: Settings, con: sqlite3.Connection, scope: Scope, base: dict, cfg: ReleaseConfig, *,
               checks: dict | None = None) -> dict:
    """首次纸面基线的五项验收。checks 可以替换任意一项（测试用），其余照常运行。"""
    checks = checks or {}
    out = {}
    for g in ("engineering", "vectors"):
        out[g] = checks[g]() if g in checks else _pytest(settings, cfg.regression_groups[g])
    if "ledger" in checks:
        out["ledger"] = checks["ledger"]()
    else:
        b = ledger_check(con, base["run_id"])
        p = ledger_check(con, paper_run_id(scope))
        out["ledger"] = {"status": "PASS" if b["status"] == p["status"] == "PASS" else "FAIL",
                         "baseline_run": b, "paper_run": p}
    out["read_only"] = checks["read_only"]() if "read_only" in checks else _pytest(settings, cfg.regression_groups["read_only"])
    if "paper_run" in checks:
        out["paper_run"] = checks["paper_run"]()
    else:
        days = paper_run_days(con, scope)
        need = cfg.min_paper_run_days
        out["paper_run"] = {"status": "PASS" if len(days) >= need else "NOT_MET", "days": len(days), "required": need,
                            "detail": f"常驻纸面运行 {paper_run_id(scope)} 按时处理过 {len(days)} 个交易日，要求至少 {need} 个"}
    return out


# ---------------------------------------------------------------- 清单
def _common(settings: Settings, con: sqlite3.Connection, scope: Scope, strategy, risk_cfg: RiskConfig) -> dict:
    vt = versions.current(scope, strategy, risk_cfg)
    head = mem.head(con)
    vt["memory_version"] = head["memory_version"] if head else None
    git = bl.git_info(settings.project_root)
    desc = bl.describe_strategy(strategy)
    return {
        "scope": {"strategy_family": scope.strategy_family, "instrument_root": scope.instrument_root,
                  "session_type": scope.session_type, "timeframe": scope.timeframe, "text": str(scope)},
        "strategy_family": scope.strategy_family,
        "version_tuple": {k: vt[k] for k in versions.FIELDS},
        "digests": {"strategy_config_sha256": desc["config_sha256"], "risk_version": risk_cfg.version,
                    "engine_code_sha256": bl.code_digest(settings.project_root),
                    "release_service_sha256": release_digest(settings.project_root),
                    "memory_version": vt["memory_version"]},
        "git_commit": git["commit"], "git_dirty": git["dirty"],
    }


def initial(settings: Settings, con: sqlite3.Connection, release_con, scope: Scope, cfg: ReleaseConfig, *, registry,
            risk_cfg: RiskConfig, baseline_id: str | None = None, checks: dict | None = None) -> dict:
    cur = pointer.resolve(release_con, scope)
    if cur is not None:
        raise ReleaseError("ACTIVE_EXISTS", f"{scope} 已有 active 版本 {cur['release_id']}，不能再建首次基线")
    base = frozen_baseline(con, str(scope), baseline_id)
    strategy = strategy_for(registry, scope, {}, "")
    core = {"scope": str(scope), "release_type": pointer.INITIAL, "baseline_id": base["baseline_id"],
            "previous_release": None, "config_diff": {}}
    expected = dev_expectations(settings, con, scope, base, risk_cfg)
    acc = acceptance(settings, con, scope, base, cfg, checks=checks)
    return {
        "release_id": release_id_of(core), "release_type": pointer.INITIAL, "previous_release": None,
        **_common(settings, con, scope, strategy, risk_cfg),
        "config_diff": {}, "candidate_id": None, "candidate_sha256": None,
        "baseline_id": base["baseline_id"], "baseline_manifest_sha256": sha(base["manifest"]),
        "evaluation": None, "shadow": None, "labels": [UNVALIDATED], "acceptance": acc,
        "rollback_target": None, "expected": expected, "rollback_trigger": rollback_trigger(expected, cfg),
        "policy_version": cfg.version, "policy_sha256": cfg.digest,
        "change_note": "首次纸面基线：把已冻结基线的正式配置建档为该 scope 的 active 纸面版本。只建档，不代表策略有效。",
        "observable_costs": ["没有策略有效性的证据（UNVALIDATED_BASELINE）：监控的预期区间来自阶段 5 开发段回放",
                             "不授予实盘权限，不替代正式评测"],
    }


def _confirm_row(con: sqlite3.Connection, candidate_id: str):
    return con.execute("SELECT evaluation_id, status, result_json, created_at_utc, protocol_id FROM evaluation_runs"
                       " WHERE candidate_id=? AND stage='CONFIRM' ORDER BY created_at_utc DESC LIMIT 1",
                       (candidate_id,)).fetchone()


def latest_shadow(con: sqlite3.Connection, candidate_id: str) -> tuple[dict | None, dict | None]:
    r = con.execute("SELECT * FROM shadow_registrations WHERE candidate_id=? ORDER BY created_at_utc DESC, rowid DESC"
                    " LIMIT 1", (candidate_id,)).fetchone()
    if r is None:
        return None, None
    reg = {k: r[k] for k in r.keys()}
    reg["plan"] = json.loads(reg.pop("plan_json"))
    res = con.execute("SELECT * FROM shadow_results WHERE shadow_id=?", (r["shadow_id"],)).fetchone()
    return reg, (None if res is None else {"status": res["status"], "result": json.loads(res["result_json"]),
                                           "result_sha256": res["result_sha256"], "created_at_utc": res["created_at_utc"]})


def base_matches(c_base: str, active: dict | None) -> bool:
    if active is None:
        return False
    if c_base == active["release_id"]:
        return True
    m = active.get("manifest") or {}
    return m.get("release_type") == pointer.INITIAL and c_base == base_release_of(m.get("baseline_id") or "")


def candidate(settings: Settings, con: sqlite3.Connection, release_con, scope: Scope, candidate_id: str,
              cfg: ReleaseConfig, *, registry, risk_cfg: RiskConfig, now: int | None = None) -> dict:
    from ..evaluation.run import candidate_digest, registration
    now = now or int(time.time())
    c = cands.get(con, candidate_id)
    if c is None or c["scope"] != str(scope):
        raise ReleaseError("NO_CANDIDATE", f"{scope} 没有候选 {candidate_id}")
    if c["status"] != "SHADOW":
        raise ReleaseError("NOT_READY", f"候选 {candidate_id} 的状态是 {c['status']}：只有影子通过的候选能准备发布清单")
    sreg, sres = latest_shadow(con, candidate_id)
    if sres is None or sres["status"] != "PASSED":
        raise ReleaseError("SHADOW_NOT_PASSED", f"候选 {candidate_id} 的影子{'还没结束' if sres is None else '结果是 ' + sres['status']}")
    if now - sres["created_at_utc"] > cfg.shadow_max_age_days * 86400:
        raise ReleaseError("EVIDENCE_EXPIRED", f"影子结果超过 {cfg.shadow_max_age_days} 天")
    ev = _confirm_row(con, candidate_id)
    if ev is None or ev["status"] != "PASS_FOR_SHADOW":
        raise ReleaseError("NOT_EVALUATED", f"候选 {candidate_id} 没有通过正式确认")
    if now - ev["created_at_utc"] > cfg.evaluation_max_age_days * 86400:
        raise ReleaseError("EVIDENCE_EXPIRED", f"正式确认结果超过 {cfg.evaluation_max_age_days} 天")
    reg = registration(con, candidate_id)
    digest = candidate_digest(c)
    if digest != reg["candidate_sha256"]:
        raise ReleaseError("MANIFEST_MISMATCH", f"候选 {candidate_id} 在评测登记之后被改动过")
    active = pointer.resolve(release_con, scope)
    if active is None:
        raise ReleaseError("NO_ACTIVE", f"{scope} 还没有 active 版本：先建立首次纸面基线")
    if not base_matches(c["base_release"], active) or sreg["base_release"] != active["release_id"]:
        raise ReleaseError("STALE_BASE", f"候选基于 {c['base_release']}（影子基线臂 {sreg['base_release']}），"
                                         f"而当前 active 是 {active['release_id']}")
    prev_diff = dict((active.get("manifest") or {}).get("config_diff") or {})
    diff = {**prev_diff, **{d["key"]: d["to"] for d in c["candidate"]["exact_diff"]}}
    if pointer.canon(diff) != pointer.canon(sreg["plan"]["cand_config_diff"]):
        raise ReleaseError("MANIFEST_MISMATCH", "候选臂在影子里用的配置与现在算出的不一致")
    core = {"scope": str(scope), "release_type": pointer.CANDIDATE, "candidate_id": candidate_id,
            "candidate_sha256": digest, "previous_release": active["release_id"], "config_diff": diff}
    rid = release_id_of(core)
    strategy = strategy_for(registry, scope, diff, rid[-8:])
    base = frozen_baseline(con, str(scope))
    dev = dev_expectations(settings, con, scope, base, risk_cfg)
    arm = (sres["result"].get("arms") or {}).get("candidate") or {}
    expected = {**dev, "source": "SHADOW+DEV_BASELINE",
                "trades_per_day": arm.get("trades_per_day") or dev["trades_per_day"],
                "setups_per_day": arm.get("setups_per_day") or dev["setups_per_day"]}
    body = c["candidate"]
    return {
        "release_id": rid, "release_type": pointer.CANDIDATE, "previous_release": active["release_id"],
        **_common(settings, con, scope, strategy, risk_cfg),
        "config_diff": diff, "candidate_id": candidate_id, "candidate_sha256": digest,
        "baseline_id": base["baseline_id"], "baseline_manifest_sha256": sha(base["manifest"]),
        "evaluation": {"registration_id": reg["registration_id"], "protocol_id": reg["protocol_id"],
                       "evaluation_id": ev["evaluation_id"], "status": ev["status"],
                       "result_sha256": hashlib.sha256(ev["result_json"].encode()).hexdigest()},
        "shadow": {"shadow_id": sreg["shadow_id"], "status": sres["status"], "plan_sha256": sreg["plan_sha256"],
                   "result_sha256": sres["result_sha256"]},
        "labels": [], "acceptance": None, "rollback_target": active["release_id"],
        "expected": expected, "rollback_trigger": rollback_trigger(expected, cfg),
        "policy_version": cfg.version, "policy_sha256": cfg.digest,
        "change_note": f"{body.get('observed_problem', '')} → {body.get('hypothesis', '')}".strip(" →"),
        "observable_costs": [f"改动：{d['key']} 从 {d['from']} 改为 {d['to']}" for d in body["exact_diff"]]
        + [f"影子期成交：基线臂 {((sres['result'].get('arms') or {}).get('baseline') or {}).get('trades')} 笔，"
           f"候选臂 {arm.get('trades')} 笔（描述性）"] + ([body["rollback_trigger"]] if body.get("rollback_trigger") else []),
    }


def acceptance_passed(m: dict) -> bool:
    return m.get("acceptance") is None or all(v.get("status") == "PASS" for v in m["acceptance"].values())


def render(m: dict, h: str) -> str:
    L = [f"# 发布清单 {m['release_id']}", "",
         f"- 类型：`{m['release_type']}`　scope：`{m['scope']['text']}`",
         f"- `manifest_hash`：`{h}`",
         f"- 上一版本（回滚目标）：`{m['previous_release'] or '无'}`",
         f"- 版本元组：{', '.join(f'{k}={v}' for k, v in m['version_tuple'].items())}",
         f"- git：`{m['git_commit']}`" + ("（工作区有未提交改动）" if m.get("git_dirty") else ""),
         f"- 配置差异：{pointer.canon(m['config_diff']) if m['config_diff'] else '无（正式配置本身）'}"]
    if m.get("labels"):
        L.append(f"- 标注：{'、'.join(m['labels'])}")
    if m.get("evaluation"):
        L.append(f"- 正式确认：`{m['evaluation']['status']}`（{m['evaluation']['evaluation_id']}）")
    if m.get("shadow"):
        L.append(f"- 影子：`{m['shadow']['status']}`（{m['shadow']['shadow_id']}）")
    if m.get("acceptance"):
        L += ["", "| 验收 | 结果 | 说明 |", "| --- | --- | --- |"]
        for k, v in m["acceptance"].items():
            L.append(f"| {k} | {v['status']} | {str(v.get('detail', v.get('problem_count', '')))[:80]} |")
    t = m["rollback_trigger"]

    def f(x):
        if x is None:
            return "不判定"
        if isinstance(x, list):
            return "[" + ", ".join(f(v) for v in x) + "]"
        return f"{x:.4g}" if isinstance(x, float) else str(x)
    L += ["", "## 回滚触发条件（监控用，单位 " + str(t.get("unit")) + "）", "",
          f"- 滚动 {t['rolling_valid_days']} 个有效日的日均净值低于 {f(t['rolling_net_per_day_floor'])}",
          f"- 激活以来回撤超过 {f(t['drawdown_limit'])}",
          f"- 成交频率超出 {f(t['trades_per_day_band'])}／日；信号频率超出 {f(t['setups_per_day_band'])}／日",
          f"- AMBIGUOUS_BAR 占比超过 {t['ambiguous_bar_share_max']:.3f}（至少 {t['ambiguous_min_trades']} 笔时判定）",
          "", "## 改动说明", "", m["change_note"] or "—", "", "## 可观察的代价", ""]
    L += [f"- {x}" for x in m["observable_costs"]]
    L += ["", "批准：`python -m trading_agent release --approve "
          + (m["candidate_id"] or m["baseline_id"]) + f" --manifest-hash {h}`（要在终端里手动输入摘要前缀）。",
          "", "这是纸面版本的发布清单，不涉及实盘，不是投资建议。"]
    return "\n".join(L) + "\n"


def write_files(settings: Settings, m: dict, h: str, out_dir: Path | None = None) -> dict:
    d = Path(out_dir) if out_dir else settings.project_root / "reports" / "releases"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{m['release_id']}.json").write_text(json.dumps({"manifest": m, "manifest_hash": h}, ensure_ascii=False,
                                                          indent=1, sort_keys=True, default=str), encoding="utf-8")
    (d / f"{m['release_id']}.md").write_text(render(m, h), encoding="utf-8")
    return {"json": str(d / f"{m['release_id']}.json"), "md": str(d / f"{m['release_id']}.md")}

