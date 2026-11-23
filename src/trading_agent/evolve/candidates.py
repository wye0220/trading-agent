"""候选：结构、校验、落盘和台账。

模板提案和导入的模型提案走同一个 validate()。候选只能用白名单内的结构化差异表达，且只改一个键；
它不能改风险上限、权限或评测协议（这些键根本不在任何白名单里），也不能自行转正。
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..engine import llm
from ..research.config import PowerConfig, ResearchConfig
from ..strategies.variants import VariantError, allowed
from .aggregate import PROMOTABLE, EvolveError, canon, sha
from .config import CHEAP, STRATEGY_TRACKS, EvolveConfig, Template

FAIL_STATUSES = ("FAIL", "FAILED")
OPEN_STATUSES = ("OBSERVATION", "PROPOSED")


class Rejected(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def candidate_id(scope_text: str, category: str, diff: list[dict]) -> str:
    return "cand_" + sha(f"{scope_text}|{category}|{canon([[d['target'], d['key'], d['to']] for d in diff])}")[:16]


def mechanism_key(c: dict) -> str:
    return f"{c['mechanism']}|{c['reason']}|{c['diff_key'] or ''}"


# ---------------------------------------------------------------- 现行配置
def _strategy_module(strategy):
    return importlib.import_module(type(strategy).__module__)


def evolvable_keys(strategy) -> tuple[str, ...]:
    """策略模块登记的可改键白名单（约定：模块级的 EVOLVABLE_KEYS）。没有登记就是什么都不能改。"""
    return tuple(getattr(_strategy_module(strategy), "EVOLVABLE_KEYS", ()))


def current_value(strategy, key: str) -> Any:
    """现行配置里这个键的值。键路径规则与配置变体相同：路过带 name 的条目列表时用 name 取条目。"""
    read = getattr(_strategy_module(strategy), "read_raw_config", None)
    if read is None:
        raise Rejected("NO_RAW_CONFIG", "这个策略没有登记可读的原始配置")
    node = read()
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list):
            hit = [i for i in node if isinstance(i, dict) and i.get("name") == part]
            if not hit:
                raise Rejected("KEY_NOT_FOUND", f"配置里没有这个键：{key}")
            node = hit[0]
        else:
            raise Rejected("KEY_NOT_FOUND", f"配置里没有这个键：{key}")
    return node


# ---------------------------------------------------------------- 组装
def protocol_id(research_cfg: ResearchConfig) -> str:
    blind = research_cfg.blind_screen_capsules * research_cfg.capsule_valid_days
    return f"blind{blind}+confirm{research_cfg.power.confirm_days}:{research_cfg.version}"


def _track_terms(track: str, p: PowerConfig) -> tuple[float, list[str]]:
    common = [f"确认窗内基线和候选各至少 {p.min_trades_per_arm} 笔成交",
              f"每边多 {p.stress_extra_ticks_per_side} tick 滑点后结论不变"]
    if track == "PRUNING_DEFENSIVE":
        return -p.margin_r_per_day, [f"净 R／日配对差的单侧置信下界不低于 −{p.margin_r_per_day}",
                                     f"最大回撤至少改善 {p.drawdown_improvement_min_pct:g}%"] + common
    if track == "ALPHA_EXPANSION":
        return p.delta_r_per_day, [f"净 R／日配对差的单侧置信下界高于 0，且平均提升不低于 {p.delta_r_per_day}",
                                   f"最大回撤增加不超过 {p.drawdown_increase_max_r:g} R"] + common
    return 0.0, ["不改变任何信号、成交或盈亏的计算"]


def from_template(t: Template, c: dict, *, strategy, base_release: str, cfg: EvolveConfig,
                  research_cfg: ResearchConfig, window_n: int) -> dict:
    """由预登记的模板和一个聚类组装候选。from 取现行配置。"""
    frm = current_value(strategy, t.key) if t.category == "strategy_rule" else None
    diff = [{"target": t.target, "key": t.key, "from": frm, "to": t.to}]
    min_effect, guards = _track_terms(t.track, research_cfg.power)
    counter = "算不出反例" if c["counter_days"] is None else f"反例 {len(c['counter_days'])} 天"
    return {
        "candidate_id": candidate_id(c["scope"], t.category, diff), "status": "PROPOSED", "scope": c["scope"],
        "category": t.category, "eval_track": t.track, "base_release": base_release,
        "evidence_event_ids": c["evidence"][-50:], "evidence_dates": c["days"][-60:],
        "counterexamples": (c["counter_sessions"] or [])[-50:],
        "observed_problem": f"{t.observed_problem}（最近 {window_n} 个 session 里出现 {len(c['days'])} 天、"
                            f"{c['occurrences']} 次；{counter}）"[:600],
        "hypothesis": t.hypothesis[:600], "exact_diff": diff, "invariants": list(t.invariants)[:10],
        "primary_metric": "net_r_per_day", "minimum_effect": min_effect, "guardrails": guards,
        "evaluation_protocol_id": protocol_id(research_cfg), "expected_tradeoff": t.expected_tradeoff[:600],
        "rollback_trigger": cfg.rollback_trigger[t.track],
    }


# ---------------------------------------------------------------- 校验
def validate(settings: Settings, con: sqlite3.Connection, cand: dict, cluster: dict | None, *, registry,
             cfg: EvolveConfig, prior_failure: dict | None = None) -> dict:
    """返回可以保存的候选（标识和状态由这里定）。不合格时抛 Rejected。"""
    errs = llm.schema_errors(settings.project_root, "Candidate", cand)
    if errs:
        raise Rejected("SCHEMA_INVALID", "；".join(errs[:3]))
    if cluster is None:
        raise Rejected("EVIDENCE_NOT_IN_CLUSTER", "提案没有对应到本次的任何聚类")
    if cand["scope"] != cluster["scope"]:
        raise Rejected("SCOPE_MISMATCH", f"候选的 scope 是 {cand['scope']}，聚类的是 {cluster['scope']}")
    d = cand["exact_diff"][0]
    cat, track = cand["category"], cand["eval_track"]
    if cat == "strategy_rule":
        if track not in STRATEGY_TRACKS:
            raise Rejected("TRACK_INVALID", "策略类候选必须标注 PRUNING_DEFENSIVE 或 ALPHA_EXPANSION")
        if d["target"] != "strategy_config":
            raise Rejected("TARGET_INVALID", "策略类候选只能改策略配置")
        family = cand["scope"].split(":")[0]
        if family not in registry:
            raise Rejected("FAMILY_NOT_REGISTERED", f"策略家族未注册：{family}")
        strategy = registry.get(family)
        if not allowed(d["key"], evolvable_keys(strategy)):
            raise Rejected("NOT_WHITELISTED", f"不在可改键的白名单内：{d['key']}")
        now = current_value(strategy, d["key"])
        if canon(now) != canon(d["from"]):
            raise Rejected("FROM_MISMATCH", f"{d['key']} 现行值是 {now!r}，提案写的是 {d['from']!r}")
        if canon(d["to"]) == canon(d["from"]):
            raise Rejected("NO_CHANGE", "差异没有改变任何值")
        try:
            registry.variant(family, {d["key"]: d["to"]}, "check")
        except VariantError as e:
            raise Rejected("NOT_WHITELISTED", str(e)) from None
        except Exception as e:  # noqa: BLE001 — 构造出非法配置
            raise Rejected("INVALID_VARIANT", f"用这个差异构造不出合法的策略配置：{e}") from None
    elif cat in ("explanation_discipline", "memory"):
        if track != CHEAP:
            raise Rejected("TRACK_INVALID", "非策略类候选走 CHEAP_NON_STRATEGY")
        if d["target"] not in ("rule_switch", "template_file") or d["key"] not in cfg.non_strategy_whitelist:
            raise Rejected("NOT_WHITELISTED", f"不在规则开关和模板文件的白名单内：{d['key']}")
        if canon(d["to"]) == canon(d["from"]):
            raise Rejected("NO_CHANGE", "差异没有改变任何值")
    else:
        raise Rejected("CATEGORY_NOT_SUPPORTED", "data_engine 类问题由日复盘立案，走引擎修复流程，不在这里提出")
    ev, days = set(cluster["evidence"]), set(cluster["days"])
    if (not cand["evidence_event_ids"] or not cand["evidence_dates"] or not set(cand["evidence_event_ids"]) <= ev
            or not set(cand["evidence_dates"]) <= days):
        raise Rejected("EVIDENCE_NOT_IN_CLUSTER", "候选引用的证据不属于它对应的聚类")
    out = dict(cand)
    out["candidate_id"] = candidate_id(cand["scope"], cat, cand["exact_diff"])
    enough = cluster["status"] == PROMOTABLE and len(set(cand["evidence_dates"])) >= cfg.min_days
    out["status"] = "PROPOSED" if enough else "OBSERVATION"
    # 失败后重提：同 scope、同机制键曾经 FAIL，必须引用那次失败并说明差异
    key = mechanism_key(cluster)
    failed = [r["candidate_id"] for r in con.execute(
        "SELECT candidate_id, body_json FROM candidates WHERE scope=? AND status IN (%s)" % ",".join("?" * len(FAIL_STATUSES)),
        (cand["scope"], *FAIL_STATUSES)) if (json.loads(r["body_json"]).get("meta") or {}).get("mechanism_key") == key]
    if out["candidate_id"] in failed:
        raise Rejected("ALREADY_FAILED", "完全相同的差异已经评测失败过")
    if failed:
        pf = prior_failure or {}
        if pf.get("candidate_id") not in failed or not pf.get("report") or not pf.get("difference"):
            raise Rejected("PRIOR_FAILURE_NOT_REFERENCED",
                           f"同一机制的候选 {'、'.join(failed)} 失败过；重新提出必须引用失败报告并说明差异")
    return out


# ---------------------------------------------------------------- 台账
def ledger(con: sqlite3.Connection, cid: str, scope_text: str, family: str | None, event: str, *, from_status=None,
           to_status=None, alpha_spent: float | None = None, detail: dict | None = None, now: int | None = None) -> None:
    """只追加。调用方负责事务。"""
    con.execute("INSERT INTO candidate_ledger (candidate_id, scope, hypothesis_family, event, from_status, to_status,"
                " alpha_spent, detail_json, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, scope_text, family, event, from_status, to_status, alpha_spent, canon(detail or {}),
                 now or int(time.time())))


def hypothesis_family(cand: dict) -> str:
    """α 记账的家族。策略类取验证轨道（每个 scope 两个家族，最保守）；其余取类别。"""
    return cand["eval_track"] if cand["category"] == "strategy_rule" else cand["category"]


def get(con: sqlite3.Connection, cid: str) -> dict | None:
    r = con.execute("SELECT * FROM candidates WHERE candidate_id=?", (cid,)).fetchone()
    if r is None:
        return None
    body = json.loads(r["body_json"])
    return {"candidate_id": cid, "scope": r["scope"], "category": r["category"], "eval_track": r["eval_track"],
            "status": r["status"], "base_release": r["base_release"], "version": r["version"],
            "candidate": body.get("candidate"), "meta": body.get("meta") or {}, "body": body}


def save(settings: Settings, con: sqlite3.Connection, cand: dict, meta: dict, *, out_dir: Path | None = None,
         now: int | None = None) -> dict:
    """写入 candidates 表和候选目录。同一候选再来一次：证据变了就刷新并记台账，否则什么都不做。"""
    now = now or int(time.time())
    cid, fam = cand["candidate_id"], hypothesis_family(cand)
    body = {"schema": "Candidate", "candidate": cand, "meta": meta}
    old = get(con, cid)
    with con:
        if old is None:
            con.execute("INSERT INTO candidates (candidate_id, scope, category, eval_track, status, base_release,"
                        " body_json, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, cand["scope"], cand["category"], cand["eval_track"], cand["status"], cand["base_release"],
                         canon(body), now, now))
            ledger(con, cid, cand["scope"], fam, "CREATED", to_status=cand["status"], now=now,
                   detail={"source": meta.get("source"), "cluster_id": meta.get("cluster_id"),
                           "evolve_id": meta.get("evolve_id"), "prior_failure": meta.get("prior_failure")})
            action = "CREATED"
        elif old["status"] == "SUPERSEDED" and old["base_release"] != cand["base_release"]:
            # 基准换了之后重新提出：同一个差异，对着新的基准
            con.execute("UPDATE candidates SET status=?, base_release=?, body_json=?, version=version+1,"
                        " updated_at_utc=? WHERE candidate_id=? AND version=?",
                        (cand["status"], cand["base_release"], canon(body), now, cid, old["version"]))
            ledger(con, cid, cand["scope"], fam, "STATUS_CHANGED", from_status="SUPERSEDED", to_status=cand["status"],
                   detail={"reason": "REPROPOSED_ON_NEW_BASE", "base_release": cand["base_release"]}, now=now)
            action = "REPROPOSED"
        elif old["status"] not in OPEN_STATUSES:
            action = "KEPT_" + old["status"]              # 已经离开提出阶段（被取用、失败、放弃、被替换）：不动它
        else:
            oc = old["candidate"] or {}
            same = (canon(oc.get("evidence_dates")) == canon(cand["evidence_dates"])
                    and oc.get("status") == cand["status"] and oc.get("base_release") == cand["base_release"])
            if same:
                action = "UNCHANGED"
            else:
                con.execute("UPDATE candidates SET status=?, base_release=?, body_json=?, version=version+1,"
                            " updated_at_utc=? WHERE candidate_id=? AND version=?",
                            (cand["status"], cand["base_release"], canon(body), now, cid, old["version"]))
                if old["status"] != cand["status"]:
                    ledger(con, cid, cand["scope"], fam, "STATUS_CHANGED", from_status=old["status"],
                           to_status=cand["status"], detail={"reason": "EVIDENCE_REFRESHED"}, now=now)
                else:
                    ledger(con, cid, cand["scope"], fam, "EVIDENCE_REFRESHED", now=now,
                           detail={"evidence_days": len(cand["evidence_dates"]), "evolve_id": meta.get("evolve_id")})
                action = "REFRESHED"
    paths = None
    if action in ("CREATED", "REFRESHED", "REPROPOSED"):
        paths = write_files(settings, cand, meta, out_dir)
    return {"candidate_id": cid, "action": action, "status": cand["status"] if action in ("CREATED", "REFRESHED", "REPROPOSED")
            else old["status"], "files": paths}


def set_status(con: sqlite3.Connection, cid: str, to_status: str, reason: str, *, now: int | None = None) -> bool:
    """改候选状态并记台账。状态没变时是空操作。"""
    c = get(con, cid)
    if c is None:
        raise EvolveError("NO_CANDIDATE", f"没有这个候选：{cid}")
    if c["status"] == to_status:
        return False
    now = now or int(time.time())
    fam = hypothesis_family(c["candidate"]) if c["candidate"] else c["category"]
    with con:
        cur = con.execute("UPDATE candidates SET status=?, version=version+1, updated_at_utc=? WHERE candidate_id=?"
                          " AND version=?", (to_status, now, cid, c["version"]))
        if cur.rowcount != 1:
            raise EvolveError("STALE", f"候选 {cid} 在此期间被改动过，请重试")
        ledger(con, cid, c["scope"], fam, "STATUS_CHANGED", from_status=c["status"], to_status=to_status,
               detail={"reason": reason}, now=now)
    return True


# ---------------------------------------------------------------- α 额度
def alpha_summary(con: sqlite3.Connection, scope_text: str, power: PowerConfig) -> dict:
    """按假设家族统计开发尝试、正式检验的登记与消耗，以及余下的 α。"""
    out = {}
    for fam in STRATEGY_TRACKS:
        n = dict(con.execute("SELECT event, COUNT(*) FROM candidate_ledger WHERE scope=? AND hypothesis_family=?"
                             " GROUP BY 1", (scope_text, fam)).fetchall())
        reg, used = n.get("FORMAL_TEST_REGISTERED", 0), n.get("FORMAL_TEST_CONSUMED", 0)
        out[fam] = {"dev_attempts": n.get("DEV_ATTEMPT", 0), "formal_registered": reg, "formal_consumed": used,
                    "formal_tests_left": max(0, power.max_formal_tests - reg), "alpha_per_test": power.alpha,
                    "alpha_family": power.alpha_family, "alpha_consumed": round(used * power.alpha, 10),
                    "alpha_remaining": round(max(0.0, power.alpha_family - reg * power.alpha), 10)}
    return out


def register_formal_test(con: sqlite3.Connection, cid: str, power: PowerConfig, *, now: int | None = None) -> dict:
    """评测角色在接触最终窗口之前登记一次正式检验。额度用完时拒绝。供阶段 8 调用。"""
    c = get(con, cid)
    if c is None or c["category"] != "strategy_rule":
        raise EvolveError("NO_CANDIDATE", f"没有这个策略类候选：{cid}")
    fam = hypothesis_family(c["candidate"])
    if con.execute("SELECT 1 FROM candidate_ledger WHERE candidate_id=? AND event='FORMAL_TEST_REGISTERED'",
                   (cid,)).fetchone():
        raise EvolveError("ALREADY_REGISTERED", f"候选 {cid} 已登记过正式检验")
    left = alpha_summary(con, c["scope"], power)[fam]["formal_tests_left"]
    if left <= 0:
        raise EvolveError("ALPHA_EXHAUSTED", f"{c['scope']} 的 {fam} 家族已用完 {power.max_formal_tests} 次正式检验")
    with con:
        ledger(con, cid, c["scope"], fam, "FORMAL_TEST_REGISTERED", detail={"alpha": power.alpha}, now=now)
    return {"candidate_id": cid, "hypothesis_family": fam, "alpha": power.alpha, "formal_tests_left": left - 1}


def consume_formal_test(con: sqlite3.Connection, cid: str, power: PowerConfig, *, now: int | None = None) -> None:
    """首次查看正式检验的结果后记为已消耗。"""
    c = get(con, cid)
    events = {r[0] for r in con.execute("SELECT event FROM candidate_ledger WHERE candidate_id=?", (cid,))}
    if c is None or "FORMAL_TEST_REGISTERED" not in events:
        raise EvolveError("NOT_REGISTERED", f"候选 {cid} 没有登记过正式检验")
    if "FORMAL_TEST_CONSUMED" in events:
        return
    with con:
        ledger(con, cid, c["scope"], hypothesis_family(c["candidate"]), "FORMAL_TEST_CONSUMED", alpha_spent=power.alpha,
               now=now)


# ---------------------------------------------------------------- 文件
def markdown(cand: dict, meta: dict) -> str:
    d = cand["exact_diff"][0]
    lines = [f"# 候选 {cand['candidate_id']}", "",
             f"- 状态：`{cand['status']}`　类别：`{cand['category']}`　验证轨道：`{cand['eval_track']}`",
             f"- scope：`{cand['scope']}`　基准：`{cand['base_release']}`",
             f"- 来源：{meta.get('source')}（聚类 `{meta.get('cluster_id')}`，机制 `{meta.get('mechanism_key')}`）"]
    if meta.get("group_id"):
        lines.append(f"- 候选组：`{meta['group_id']}`（{'、'.join(meta.get('group_scopes') or [])} 各自分别评测）")
    if meta.get("fixture"):
        lines.append("- **FIXTURE**：证据来自合成数据，不进队列，不可晋级")
    lines += ["", "## 改动", "", f"`{d['key']}`：`{json.dumps(d['from'], ensure_ascii=False)}` → "
              f"`{json.dumps(d['to'], ensure_ascii=False)}`（{d['target']}，只改这一个键）", "",
              "## 观察到的现象", "", cand["observed_problem"], "", "## 假设（待检验，不是结论）", "", cand["hypothesis"], "",
              "## 证据", "", f"- 证据日 {len(cand['evidence_dates'])} 天：{cand['evidence_dates'][0]} 至 "
              f"{cand['evidence_dates'][-1]}", f"- 证据事件 {len(cand['evidence_event_ids'])} 条（最多列 50 条，见 JSON）",
              f"- 反例 {len(cand.get('counterexamples') or [])} 个 session"
              + ("" if meta.get("counter_share") is not None else "（这个机制算不出反例）"), "",
              "## 评测", "", f"- 主指标：`{cand['primary_metric']}`，最小效应 {cand.get('minimum_effect')}",
              f"- 协议：`{cand.get('evaluation_protocol_id')}`"]
    lines += [f"- 护栏：{g}" for g in cand.get("guardrails") or []]
    lines += [f"- 不变量：{i}" for i in cand.get("invariants") or []]
    lines += ["", "## 预期的代价", "", cand.get("expected_tradeoff") or "—", "", "## 回滚条件", "",
              cand["rollback_trigger"], "", "候选只是待评测的改动，不经独立评测和人工批准不会生效。", ""]
    return "\n".join(lines)


def write_files(settings: Settings, cand: dict, meta: dict, out_dir: Path | None = None) -> dict:
    d = (Path(out_dir) if out_dir else settings.project_root / "candidates") / cand["scope"].replace(":", "_")
    d.mkdir(parents=True, exist_ok=True)
    j, m = d / f"{cand['candidate_id']}.json", d / f"{cand['candidate_id']}.md"
    j.write_text(json.dumps({"candidate": cand, "meta": meta}, ensure_ascii=False, indent=2), encoding="utf-8")
    m.write_text(markdown(cand, meta), encoding="utf-8")
    return {"json": str(j), "markdown": str(m)}
