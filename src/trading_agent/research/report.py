"""基线报告与功效报告：组装数据，输出 Markdown、JSON 和逐笔 CSV。"""
from __future__ import annotations

import csv
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from .. import sessions
from ..config import Scope, Settings
from ..engine.config import RiskConfig
from . import baseline as bl
from . import metrics, splits
from .config import ResearchConfig

SEGMENTS = (("development", "开发段", splits.DEVELOPMENT), ("forward_validation", "向前验证段", splits.FORWARD))


def scope_slug(scope: Scope | str) -> str:
    return str(scope).replace(":", "_")


def paths(settings: Settings, scope: Scope | str, out_dir: Path | None = None) -> dict:
    d = Path(out_dir) if out_dir else settings.project_root / "reports"
    s = scope_slug(scope)
    return {"baseline_md": d / f"baseline_{s}.md", "baseline_json": d / f"baseline_{s}.json",
            "trades_csv": d / f"baseline_trades_{s}.csv", "power_md": d / f"baseline_power_{s}.md",
            "power_json": d / f"baseline_power_{s}.json"}


# ---------------------------------------------------------------- 组装
def measured_days(reg: dict, research_cfg: ResearchConfig) -> dict:
    """各段用于度量的交易日：有效日，去掉数据起点后的预热日。"""
    every = sorted(d[0] for s in reg["segments"] if s["kind"] in (splits.DEVELOPMENT, splits.FORWARD) for d in s["days"])
    warm = set(every[:research_cfg.warmup_trading_days])
    out = {}
    for key, _, kind in SEGMENTS:
        allv = splits.segment_days(reg, kind, valid_only=False)
        valid = [d for d in splits.segment_days(reg, kind) if d not in warm]
        out[key] = {"all": allv, "valid": valid, "warmup": [d for d in allv if d in warm]}
    out["total"] = {"all": every, "valid": sorted(out["development"]["valid"] + out["forward_validation"]["valid"]),
                    "warmup": sorted(warm)}
    return out


def collect(settings: Settings, con: sqlite3.Connection, scope: Scope, base: dict, research_cfg: ResearchConfig,
            risk_cfg: RiskConfig) -> dict:
    reg, run_id = base["registry"], base["run_id"]
    inst = settings.instrument(scope.instrument_root)
    kw = dict(tick_value=inst.tick_value_usd, research_unit=risk_cfg.research_risk_unit_usd,
              stress_ticks_per_side=research_cfg.power.stress_extra_ticks_per_side)
    meta = metrics.setup_metadata(con, run_id)
    trades = metrics.load_trades(settings, con, scope, run_id, risk_cfg, meta)
    days = measured_days(reg, research_cfg)
    segs = {}
    for key in ("development", "forward_validation", "total"):
        valid = days[key]["valid"]
        if not days[key]["all"]:
            continue
        ev = bl.event_summary(con, run_id, set(valid))
        segs[key] = {
            "first_day": days[key]["all"][0], "last_day": days[key]["all"][-1],
            "trading_days": len(days[key]["all"]), "valid_days": len(valid), "warmup_days": len(days[key]["warmup"]),
            "coverage": len(valid) / len(days[key]["all"]),
            "opportunities": ev["setups"], "decisions": ev["decisions"],
            "skip_reasons": ev["skip_reasons"], "block_reasons": ev["block_reasons"], "sub_bar_days": ev["sub_bar_days"],
            "orders": metrics.order_summary(con, run_id, valid),
            "fixed": metrics.summarize(trades, valid, **kw),
            "risk_sized": metrics.risk_sized(trades, valid, risk_cfg, **kw)}
    total = days["total"]["valid"]
    groups = {}
    for label, path in research_cfg.groups_for(scope.strategy_family).items():
        groups[label] = metrics.group_table(trades, total, lambda t, p=path: metrics._path(t["metadata"], p), **kw)
    groups["品种"] = metrics.group_table(trades, total, lambda t: scope.instrument_root, **kw)
    groups["时段"] = metrics.group_table(trades, total, lambda t: scope.session_type, **kw)
    groups["合约"] = metrics.group_table(trades, total, lambda t: t["contract_id"], **kw)
    groups["年份"] = metrics.group_table(trades, total, lambda t: t["trading_day"][:4], **kw)
    groups["出场原因"] = metrics.group_table(trades, total, lambda t: t["exit_reason"], **kw)
    groups["入场时段（纽约时间）"] = metrics.group_table(
        trades, total, lambda t: metrics._bucket(t["entry_time_utc"], research_cfg.time_buckets_et), **kw)
    groups["子 K 周期"] = metrics.group_table(trades, total, lambda t: t["sub_bar_timeframe"], **kw)
    totalset = set(total)
    data = base["manifest"]["data"]
    flags = list(base["flags"])
    return {
        "baseline_id": base["baseline_id"], "scope": str(scope), "status": base["status"], "flags": flags,
        "fixture_only": "FIXTURE_ONLY" in flags,
        "strategy_validity": "EVIDENCE_INSUFFICIENT" if "FIXTURE_ONLY" in flags else "NOT_ASSESSED",
        "manifest": base["manifest"], "digests": base["digests"], "registry": {
            "registry_id": reg["registry_id"], "summary": reg["summary"], "definition": reg["definition"]},
        "segments": segs, "groups": groups,
        "excluded_trades": sum(1 for t in trades if t["trading_day"] not in totalset),
        "available_at": {"signal_bars": data["signal_bars"], "estimated": data["signal_bars_estimated_available_at"],
                         "delay_seconds": data["available_at_delay_seconds"],
                         "activation_delay_seconds": base["manifest"]["engine"]["replay_activation_delay_seconds"]},
        "trades": trades, "measured_days": {k: v["valid"] for k, v in days.items()},
    }


# ---------------------------------------------------------------- 格式
def _usd(x) -> str:
    return "—" if x is None else f"{float(x):,.2f}"


def _num(x, nd=3) -> str:
    return "—" if x is None else f"{float(x):.{nd}f}"


def _pct(x) -> str:
    return "—" if x is None else f"{float(x) * 100:.1f}%"


def _table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out + [""]


def _pf(s: dict) -> str:
    if s["profit_factor"] is None:
        return "不可估" if s["trades"] else "—"
    return _num(s["profit_factor"], 2)


def _counts(d: dict, top: int = 12) -> str:
    items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    text = "，".join(f"`{k}` {v:,}" for k, v in items[:top])
    return (text + (f"，其余 {len(items) - top} 项" if len(items) > top else "")) or "无"


SEG_LABEL = {"development": "开发段", "forward_validation": "向前验证段", "total": "合计"}


def render_baseline(r: dict) -> str:
    m, segs = r["manifest"], r["segments"]
    keys = [k for k in ("development", "forward_validation", "total") if k in segs]
    col = lambda f: [f(segs[k]) for k in keys]          # noqa: E731
    L = [f"# 基线报告 · {r['scope']}", ""]
    if r["fixture_only"]:
        L += ["> **FIXTURE_ONLY**：本报告只用了合成行情，数字只说明流程能跑通。策略有效性：`EVIDENCE_INSUFFICIENT`。", ""]
    if r["status"] != "FROZEN":
        L += [f"> **{r['status']}**：两次回放结果不一致，这份基线不能作为比较基准。", ""]
    other = [f for f in r["flags"] if f != "FIXTURE_ONLY"]
    if other:
        L += ["> 标记：" + "、".join(f"`{f}`" for f in other), ""]
    st = m["strategy"]
    L += ["## 冻结清单", ""]
    L += _table(["项目", "值"], [
        ["baseline_id", f"`{r['baseline_id']}`"], ["状态", f"`{r['status']}`"],
        ["策略", f"{st['strategy_family']} {st['strategy_version']}，配置摘要 `{st['config_sha256'][:12]}`"],
        ["已启用形态", "、".join(st["enabled_setups"]) or "—"],
        ["指标定义", "，".join(f"{k}={v}" for k, v in sorted(m["indicators"].items()))],
        ["引擎版本", m["engine_version"]], ["风控版本", m["risk"]["risk_version"]],
        ["费用", f"{m['risk']['fee_amount_usd'] or '未配置'}（{m['risk']['fee_mode']}）"],
        ["滑点", f"入场 {m['risk']['entry_slippage_ticks']} tick，止损与强平 {m['risk']['stop_slippage_ticks']} tick"],
        ["研究风险单位", f"${m['risk']['research_risk_unit_usd']}" if m["risk"]["research_risk_unit_usd"] else "未配置"],
        ["时段与周期", f"{m['session']['session_type']} {m['session']['timeframe']}，{m['session']['calendar_version']}，"
                  f"{m['session']['roll_rule_version']}"],
        ["行情清单", f"{m['data']['first_day']} 至 {m['data']['last_day']}，{m['data']['days']} 个交易日，"
                 f"{m['data']['bars']:,} 根（含全部 revision），摘要 `{m['data']['sha256'][:12]}`"],
        ["切分登记", f"`{r['registry']['registry_id']}`（基线只绑定开发段和向前验证段）"],
        ["代码摘要", f"`{m['code_sha256'][:12]}`" + (f"，git `{(m.get('git') or {}).get('commit', '')[:10]}`"
                                               if (m.get("git") or {}).get("commit") else "")],
        ["提示词摘要", "无（回放不调用模型）"], ["随机种子", "无（回放是确定性的）"],
        ["跨窗口持仓", m["carry_over"]],
        ["预热", f"{m['warmup']['rule']}；数据起点后的前 {m['warmup']['excluded_trading_days']} 个交易日不计入度量"],
    ])
    dg = r["digests"]
    if dg:
        L += ["## 复现校验", "",
              f"同样的输入回放两次：{'逐笔、逐张订单、逐条决策的摘要全部相同。' if dg.get('identical') else '摘要不一致。'}", ""]
        L += _table(["对象", "条数", "第一次", "第二次"], [
            [name, f"{dg['base']['counts'][k]:,}", f"`{dg['base'][k][:16]}`", f"`{dg['verify'][k][:16]}`"]
            for name, k in (("交易", "trades"), ("订单", "orders"), ("决策记录", "decisions"))])

    reg = r["registry"]["summary"]
    L += ["## 数据切分", ""]
    L += _table(["段", "日期", "交易日", "有效日（计入度量）", "预热日"], [
        [SEG_LABEL[k], f"{segs[k]['first_day']} 至 {segs[k]['last_day']}", segs[k]["trading_days"], segs[k]["valid_days"],
         segs[k]["warmup_days"]] for k in keys])
    L += [f"截止日之后登记了 {reg['capsules']} 个胶囊（{reg['capsule_valid_days']} 个有效日，全部封存），"
          f"余量 {reg['remainder_valid_days']} 日。按默认流程可评测 {reg['candidates_supported']} 个候选。", ""]

    L += ["## 机会与成交", ""]
    L += _table(["指标"] + [SEG_LABEL[k] for k in keys], [
        ["数据覆盖率（有效日 ÷ 交易日）"] + col(lambda s: _pct(s["coverage"])),
        ["机会数（SETUP_VALID）"] + col(lambda s: f"{s['opportunities']:,}"),
        ["订单数"] + col(lambda s: f"{s['orders']['orders']:,}"),
        ["成交数（已完结交易）"] + col(lambda s: f"{s['fixed']['trades']:,}"),
        ["未成交的订单"] + col(lambda s: f"{s['orders']['orders'] - s['orders']['by_status'].get('FILLED', 0):,}"),
        ["无交易日"] + col(lambda s: f"{s['fixed']['no_trade_days']:,}（{_pct(s['fixed']['no_trade_days'] / s['valid_days'] if s['valid_days'] else None)}）"),
        ["日均成交"] + col(lambda s: _num(s["fixed"]["trades_per_day"])),
    ])
    L += ["## 收益与成本（固定 1 手）", ""]
    L += _table(["指标"] + [SEG_LABEL[k] for k in keys], [
        ["毛盈亏（美元）"] + col(lambda s: _usd(s["fixed"]["gross_usd"])),
        ["券商费用（美元）"] + col(lambda s: _usd(s["fixed"]["fees_usd"])),
        ["模型滑点成本（美元，已在成交价里）"] + col(lambda s: _usd(s["fixed"]["slippage_cost_usd"])),
        ["净盈亏（美元）"] + col(lambda s: _usd(s["fixed"]["net_usd"])),
        ["净 R／笔（研究风险单位）"] + col(lambda s: _num(s["fixed"]["net_r_per_trade"])),
        ["净 R／笔（按各笔初始风险，辅助）"] + col(lambda s: _num(s["fixed"]["net_r_initial_per_trade"])),
        ["净 R／日"] + col(lambda s: _num(s["fixed"]["net_r_per_day"], 4)),
        ["每日净 R 标准差"] + col(lambda s: _num(s["fixed"]["daily_r_std"])),
        ["胜率"] + col(lambda s: _pct(s["fixed"]["win_rate"])),
        ["PF"] + col(lambda s: _pf(s["fixed"])),
        ["平均盈利／平均亏损（美元）"] + col(lambda s: f"{_usd(s['fixed']['avg_win_usd'])} ／ {_usd(s['fixed']['avg_loss_usd'])}"),
        [f"每边多 {segs[keys[0]]['fixed']['stress']['extra_ticks_per_side']} tick 滑点后的净盈亏（美元）"]
        + col(lambda s: _usd(s["fixed"]["stress"]["net_usd"])),
    ])
    d = lambda x, k="mean", nd=1: "—" if x is None else _num(x[k], nd)      # noqa: E731
    L += ["## 风险", ""]
    L += _table(["指标"] + [SEG_LABEL[k] for k in keys], [
        ["最大回撤（按日，美元）"] + col(lambda s: _usd(s["fixed"]["max_drawdown_usd"])),
        ["最大回撤（按日，R）"] + col(lambda s: _num(s["fixed"]["max_drawdown_r"], 2)),
        ["最大回撤（逐笔，美元）"] + col(lambda s: _usd(s["fixed"]["max_drawdown_trade_usd"])),
        ["MAE 平均／中位（tick）"] + col(lambda s: f"{d(s['fixed']['mae_ticks'])} ／ {d(s['fixed']['mae_ticks'], 'median')}"),
        ["MFE 平均／中位（tick）"] + col(lambda s: f"{d(s['fixed']['mfe_ticks'])} ／ {d(s['fixed']['mfe_ticks'], 'median')}"),
        ["MAE／MFE 平均（初始风险的倍数）"] + col(lambda s: f"{d(s['fixed']['mae_r_initial'], nd=2)} ／ {d(s['fixed']['mfe_r_initial'], nd=2)}"),
        ["持仓时长 平均／中位（分钟）"] + col(lambda s: f"{d(s['fixed']['holding_minutes'])} ／ {d(s['fixed']['holding_minutes'], 'median')}"),
        ["`AMBIGUOUS_BAR` 占比"] + col(lambda s: _pct(s["fixed"]["ambiguous_bar_share"])),
        ["`AMBIGUOUS_ENTRY` 占比"] + col(lambda s: _pct(s["fixed"]["ambiguous_entry_share"])),
        ["跳空越过止损：笔数／超计划损失（美元）"] + col(lambda s: f"{s['fixed']['gap_through_stop']['trades']} ／ {_usd(s['fixed']['gap_through_stop']['excess_usd'])}"),
        ["只有 5 分钟子 K 的成交（时序不明）"] + col(lambda s: s["fixed"]["coarse_sub_bar_trades"]),
    ])
    tot = segs["total"]
    L += ["全部标记计数（合计）：" + _counts(tot["fixed"]["flags"]), "",
          "出场原因（合计）：" + _counts(tot["fixed"]["exit_reasons"]), ""]

    L += ["## 风险规模结果", ""]
    rs = tot["risk_sized"]
    if rs["status"] != "OK":
        L += [f"未配置：{rs['reason']}。固定 1 手的结果见上。", ""]
    else:
        lim = rs["limits"]
        L += [f"限额：单笔预算 ${lim['budget_per_trade_usd']}，日损上限 ${lim['daily_loss_limit_usd']}，"
              f"最大 {lim['max_contracts']} 手，每日最多 {lim['max_stops_per_day']} 次止损。"
              "这是对固定 1 手交易按预算重新定手数的结果，信号和成交路径相同。", ""]
        L += _table(["指标"] + [SEG_LABEL[k] for k in keys], [
            ["成交数"] + col(lambda s: s["risk_sized"]["summary"]["trades"]),
            ["因预算不够 1 手而跳过"] + col(lambda s: s["risk_sized"]["skipped_too_small"]),
            ["因当日停手而跳过"] + col(lambda s: s["risk_sized"]["skipped_after_halt"]),
            ["净盈亏（美元）"] + col(lambda s: _usd(s["risk_sized"]["summary"]["net_usd"])),
            ["最大回撤（按日，美元）"] + col(lambda s: _usd(s["risk_sized"]["summary"]["max_drawdown_usd"])),
            ["胜率"] + col(lambda s: _pct(s["risk_sized"]["summary"]["win_rate"])),
            ["PF"] + col(lambda s: _pf(s["risk_sized"]["summary"])),
        ])

    L += ["## 等待、被拒与未触发", "",
          "等待或过滤原因（合计，按决策记录计）：" + _counts(tot["skip_reasons"]), "",
          "有信号但没有挂单的原因（合计）：" + _counts(tot["block_reasons"]), "",
          "未成交订单的去向（合计）：" + _counts(tot["orders"]["unfilled_reasons"]), ""]

    L += ["## 分组（合计，固定 1 手）", ""]
    for label, table in r["groups"].items():
        if not table:
            continue
        L += [f"### {label}", ""]
        L += _table(["组", "成交", "净盈亏（美元）", "胜率", "PF", "净 R／笔", "净 R 合计"], [
            [k, v["trades"], _usd(v["net_usd"]), _pct(v["win_rate"]),
             "不可估" if v["profit_factor"] is None else _num(v["profit_factor"], 2),
             _num(v["net_r_per_trade"]), _num(v["net_r_total"], 2)] for k, v in table.items()])

    av = r["available_at"]
    L += ["## 模拟时效假设", "",
          f"信号 K 共 {av['signal_bars']:,} 根，其中 {av['estimated']:,} 根（{_pct(av['estimated'] / av['signal_bars'] if av['signal_bars'] else None)}）"
          f"的可用时点是估计值：历史文件没有到达时刻，按「收盘 + {av['delay_seconds']} 秒」估计。"
          f"回放的决策时点 = 信号 K 的可用时点 + {av['activation_delay_seconds']} 秒。"
          "真实采集的延迟若更长，下一根 K 开头几秒内的触发会来不及挂单，成交数会比这里少。", ""]
    if r["excluded_trades"]:
        L += [f"另有 {r['excluded_trades']} 笔交易发生在预热日或无效日，没有计入以上任何数字。", ""]
    if r["fixture_only"]:
        L += ["## 真实行情的缺口", "",
              "运行库里没有任何真实行情。要得到可用的基线，需要：", "",
              "- 字段：实际合约代码、开高低收、成交量、K 线起始时刻；最好带到达时刻。",
              "- 频率：1 分钟线（用于判定同一根 5 分钟线内的成交先后），或至少 5 分钟线。",
              "- 时段：RTH 全时段；夜盘（ETH）用于隔夜高低点和开盘背景。",
              "- 覆盖年限：开发段之外，每评测一个候选还要 60 个从未见过的有效交易日。", ""]
    L += ["## 说明", "",
          "- 这是纸面模拟的结果，不是投资建议，也不代表未来表现。",
          "- 度量只计有效交易日（当日实际合约 RTH 完整且来源质量未降级）；零交易日计入分母。",
          "- MAE／MFE 用持仓期间的子 K 高低点事后度量，入场所在的那一根因先后不明不计入。", ""]
    return "\n".join(L)


def render_power(scope: str, p: dict, cfg) -> str:
    unit = p["unit"]
    b = p["baseline"]
    L = [f"# 基线功效测算 · {scope}", ""]
    if "FIXTURE_ONLY" in p["flags"]:
        L += ["> **FIXTURE_ONLY**：输入是合成数据，结论一律为 `EVIDENCE_INSUFFICIENT`。", ""]
    L += ["> 这里的扰动是为规划而预登记的合成改动，只用来估计配对日差的波动。它们不是候选，结果也不是任何改动能盈利的证据。", ""]
    L += ["## 结论", ""]
    names = {"PRUNING_DEFENSIVE": "Track A（剪枝防守）", "ALPHA_EXPANSION": "Track B（结构扩展）"}
    L += _table(["轨道", "规划效应（R／日）", "40 日整体通过率", "短窗正式资格", "原因"], [
        [names[t], _num(v["planning_effect"]), _pct(v["joint_pass_rate"]), f"`{v['eligibility']}`",
         "；".join(v["reasons"]) or "—"] for t, v in p["tracks"].items()])
    av = p["availability"]
    if av:
        if av["required_days"] is None:
            L += ["所需确认日数无法估计。", ""]
        elif av["sufficient"]:
            L += [f"独立正态近似下，检验 δ={_num(av['delta'])} R／日约需 {av['required_days']:.0f} 个确认日，"
                  f"截止日之后登记的有效日有 {av['available_days']} 个。", ""]
        else:
            L += [f"**可用数据不足以检验 δ={_num(av['delta'])} R／日**：独立正态近似下约需 {av['required_days']:.0f} 个确认日，"
                  f"截止日之后登记的有效日只有 {av['available_days']} 个。按当前数据可检验的最小 δ 约为 "
                  f"{_num(av['min_testable_delta'])} R／日。", ""]
        if av["required_days"] and av["required_days"] > p["confirm_days"]:
            L += [f"所需日数大于默认的 {p['confirm_days']} 日确认窗。要用更长的确认，必须在查看任何相关确认结果之前另立协议。", ""]
    L += ["## 开发段实测", ""]
    L += _table(["指标", "值"], [
        ["有效交易日", b["days"]], ["成交数", b["trades"]], ["日均成交", _num(b["trades_per_day"])],
        ["零交易日占比", _pct(b["zero_trade_day_share"])],
        [f"每日净 {unit} 均值", _num(b["net_per_day_mean"], 4)], [f"每日净 {unit} 标准差", _num(b["net_per_day_std"])],
        ["每日净值的一阶自相关", _num(b["autocorr_lag1"])],
        [f"{p['confirm_days']} 日窗口内的期望成交数", _num(p["expected_trades_in_window"], 1)],
    ])
    L += ["## 设置", "",
          f"- 单侧 α：家族 {p['alpha_family']} ÷ 预登记 {p['max_formal_tests']} 次正式检验 = {p['alpha']}。规划功效目标 {_pct(p['power_target'])}。",
          f"- 模拟：连续交易日循环区块 bootstrap，区块 {cfg.block_length_days} 日，{cfg.simulations} 个确认窗，"
          f"每窗 {cfg.inner_resamples} 次内层重采样，种子 {cfg.seed}。",
          f"- Track A 门槛：配对净 R／日的置信下界 > −{cfg.margin_r_per_day}，且最大回撤至少改善 {cfg.drawdown_improvement_min_pct:g}%。",
          f"- Track B 门槛：平均配对提升 ≥ {cfg.delta_r_per_day}，置信下界 > 0，最大回撤增量 ≤ {cfg.drawdown_increase_max_r} R。",
          f"- 两条轨都要求：每边多 {cfg.stress_extra_ticks_per_side} tick 滑点后候选净结果 > 0 且不劣于基线；"
          f"基线和候选各不少于 {cfg.min_trades_per_arm} 笔成交。",
          "- 做法：把观察到的配对差平移到设定的真实效应，再看门槛通过的比例。平移假设效应均匀落在每个交易日上，是近似。", ""]
    L += ["## 预登记扰动的配对日差", ""]
    L += _table(["扰动", "轨道", "说明", "成交数", "平均差", "标准差", "一阶自相关", "有差异的日数",
                 f"{p['confirm_days']} 日可检出（正态近似）", "20 日可检出"], [
        [f"`{v['id']}`", "A" if v["track"] == "PRUNING_DEFENSIVE" else "B", v.get("note") or "", v["variant"]["trades"],
         _num(v["paired"]["mean"], 4), _num(v["paired"]["std"]), _num(v["paired"]["autocorr_lag1"]),
         v["paired"]["days_changed"], _num(v["normal_approx"]["detectable_confirm"]),
         _num(v["normal_approx"]["detectable_20d"])] for v in p["variants"]])
    L += ["20 日一栏只用来说明窗口大小，20 日盲筛不做正式接受检验。", ""]
    for v in p["variants"]:
        sim = v["simulation"]
        L += [f"## 模拟 · `{v['id']}`（{names[v['track']]}）", ""]
        if sim is None:
            L += ["数据太少，没有模拟。", ""]
            continue
        L += _table(["真实效应（R／日）", "置信界条件", "平均提升条件", "回撤条件", "滑点压力", "最低成交数", "整体通过率",
                     "不含回撤条件", "只看统计条件"], [
            [_num(x["true_effect"]), _pct(x["ci"]), _pct(x["effect"]) if v["track"] == "ALPHA_EXPANSION" else "不适用",
             _pct(x["drawdown"]), _pct(x["stress"]), _pct(x["min_trades"]), f"**{_pct(x['joint'])}**",
             _pct(x["joint_without_drawdown"]), _pct(x["statistical_only"])] for x in sim["rows"]])
        me, ms = sim["min_effect_for_target"], sim["min_effect_statistical_only"]
        L += [f"整体通过率达到 {_pct(p['power_target'])} 所需的最小真实效应："
              + (f"约 {_num(me)} R／日。" if me is not None else "在模拟的范围内达不到。")
              + " 只看统计条件时：" + (f"约 {_num(ms)} R／日。" if ms is not None else "在模拟的范围内达不到。"), ""]
    return "\n".join(L)


# ---------------------------------------------------------------- 输出
def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o).__name__)


def write_baseline(r: dict, p: dict) -> None:
    p["baseline_md"].parent.mkdir(parents=True, exist_ok=True)
    p["baseline_md"].write_text(render_baseline(r), encoding="utf-8")
    slim = {k: v for k, v in r.items() if k not in ("trades", "measured_days")}
    p["baseline_json"].write_text(json.dumps(slim, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    cols = ["trading_day", "contract_id", "setup_id", "side", "quantity", "entry_time_et", "entry_ticks", "exit_time_et",
            "exit_ticks", "exit_reason", "initial_risk_ticks", "pnl_ticks", "gross_usd", "fees_usd", "net_usd",
            "r_initial", "r_research", "mae_ticks", "mfe_ticks", "holding_seconds", "sub_bar_timeframe",
            "gap_excess_ticks", "flags", "metadata"]
    with open(p["trades_csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in r["trades"]:
            row = {**t, "entry_time_et": sessions.et(t["entry_time_utc"]).strftime("%Y-%m-%d %H:%M:%S"),
                   "exit_time_et": sessions.et(t["exit_time_utc"]).strftime("%Y-%m-%d %H:%M:%S"),
                   "flags": " ".join(t["flags"]),
                   "metadata": json.dumps({k: t["metadata"].get(k) for k in ("setup", "label", "side", "target_rule")
                                           if k in t["metadata"]}, ensure_ascii=False)}
            w.writerow([row[c] for c in cols])


def write_power(scope: str, pw: dict, cfg, p: dict) -> None:
    p["power_md"].parent.mkdir(parents=True, exist_ok=True)
    p["power_md"].write_text(render_power(scope, pw, cfg), encoding="utf-8")
    p["power_json"].write_text(json.dumps(pw, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
