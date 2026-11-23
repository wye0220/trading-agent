"""日复盘的 Markdown。"""
from __future__ import annotations

from .. import sessions

CAT = {"DATA_ENGINE_ERROR": "数据／引擎错误", "RULE_EXECUTION_DEVIATION": "规则执行偏差", "LOSS_BY_RULE": "按规则亏损",
       "PROFIT_BY_RULE": "按规则盈利", "OUT_OF_RULE_OBSERVATION": "规则外观察"}


def _bullets(items) -> list[str]:
    return [f"- {x}" for x in items] or ["- 无。"]


def _pct(x) -> str:
    return "—" if x is None else f"{x:.0%}"


def markdown(review: dict, detail: dict) -> str:
    st, man = detail["strategy_track"], detail["manual_track"]
    L = [f"# 日复盘 · {review['scope']} · {review['trading_day']}", "",
         f"session `{review['session_id']}`，运行模式 `{detail['run_mode']}`，规则版本 `{detail['rules_version']}`。"
         + ("  **[FIXTURE]**" if (detail.get("data") or {}).get("fixture") else ""), "",
         "> 这是纸面模拟和执行记录的复盘，不是投资建议。结论由规则得出，每条都带证据编号。", "",
         "## 一、当时知道的事实", "", *_bullets(review["facts_known_then"]), "",
         "## 二、证据不足的判断", "", *_bullets(review["insufficient_evidence"]), "",
         "## 三、事后信息", "", "以下内容当时不可能知道，只用于理解结果，不能用来评价当时的决定。", "",
         *_bullets(review["hindsight"]), "",
         "## 四、策略轨（纸面）", ""]
    if detail.get("run_id") is None:
        L += ["这是只含人工轨的复盘，没有对应的策略 session。"]
    else:
        L += [
         f"- 机会：允许 {st['allowed']}，拦截 {st['blocked']}"
         + ("（" + "、".join(f"`{k}` {v}" for k, v in sorted(st["blocked_reasons"].items())) + "）" if st["blocked_reasons"] else "") + "。",
         f"- 订单：" + ("、".join(f"`{k}` {v}" for k, v in sorted(st["orders"].items())) or "无") + "。",
         f"- 交易 {st['trades']} 笔，净 {st['net_usd']} 美元" + (f"，{st['net_r']:.3f} R" if st["net_r"] is not None else "")
         + (f"，胜率 {_pct(st['win_rate'])}" if st["win_rate"] is not None else "") + "。"]
    if st["flags"]:
        L.append("- 标记：" + "、".join(f"`{k}` {v}" for k, v in sorted(st["flags"].items())) + "。")
    for label, table in st["groups"].items():
        L.append(f"- 按{label}：" + "；".join(f"{k} {v['trades']} 笔 净 {v['net_usd']}" for k, v in table.items()) + "。")
    L += ["", "## 五、人工轨", ""]
    if man is None:
        L += ["没有指定账户，本次复盘不含人工轨。", ""]
    else:
        L += [f"- **计划覆盖率 {'无成交' if man['plan_coverage'] is None else _pct(man['plan_coverage'])}**"
              f"（开仓 {man['opens']}，有事前计划 {man['opens_planned']}，无计划 {man['opens_unplanned']}，待核对 {man['opens_link_unknown']}）"
              + ("。低于阈值：以下纪律结论样本不足。" if man["sample_insufficient"] else "。"),
              f"- 计划执行：可判定 {man['adherence_decidable']} 次，违规 {man['plan_adherence'].get('VIOLATION', 0)} 次"
              f"（{_pct(man['violation_rate'])}）；无法判定 {man['adherence_unknown']} 次，单列不入分母。",
              "- 策略一致性：" + ("、".join(f"`{k}` {v}" for k, v in sorted(man["strategy_alignment"].items())) or "无平仓") + "。",
              "- 纪律事件：" + ("、".join(f"`{k}` {v}" for k, v in sorted(man["discipline_events"].items())) or "无") + "。",
              f"- 已实现 {man['realized_usd']} 美元。", ""]
        if man["alerts"]:
            L += ["盘中提醒回顾：", ""]
            for a in man["alerts"]:
                took = "未处理" if a["seconds_to_resolve"] is None else f"{a['seconds_to_resolve'] // 60} 分钟后解除"
                L.append(f"- {sessions.et(a['raised_at_utc']).strftime('%H:%M')} `{a['rule']}`：{a.get('message') or ''} "
                         f"处理：{took}；之后同一规则又触发 {a['repeated_after']} 次。[{a['alert_id']}]")
            L.append("")
    L += ["## 六、归因", ""]
    counts = detail["attribution_counts"]
    L.append("、".join(f"{CAT[k]} {counts[k]}" for k in CAT if counts.get(k)) or "没有可归因的结果。")
    L.append("")
    for a in review["attribution"]:
        L.append(f"- {CAT[a['category']]}：{a.get('note', '')} [{a['event_id']}]")
    L += ["", "## 七、观察", ""]
    for o in review["observations"]:
        ev = "、".join(o["evidence_event_ids"][:5]) + ("…" if len(o["evidence_event_ids"]) > 5 else "")
        L.append(f"- `{o['kind']}` {o['mechanism']}" + (f" [{ev}]" if ev else ""))
    L += ["", "观察只登记发生了什么。策略和纪律类的观察交给跨日聚合，不在当日产生候选。", ""]
    if detail.get("candidates"):
        L += ["数据／引擎问题已立案（走引擎修复流程）：" + "、".join(f"`{c['candidate_id']}`（{c['mechanism']}）"
                                                    for c in detail["candidates"]) + "。", ""]
    if detail.get("incident_drafts"):
        L += ["## 八、事故草稿", ""]
        for i in detail["incident_drafts"]:
            L.append(f"- `{i['incident_id']}` {i['kind']}：证据 {'、'.join(i['evidence_event_ids'][:3])}，"
                     f"原始事件 {i['raw_events']} 条，缺口 {i['gaps']} 处，"
                     f"{'有' if i['has_checkpoint'] else '没有'}检查点。等你确认后才进回归库。")
        L.append("")
    if detail.get("note_responses"):
        L += ["## 九、对备注的回应", ""]
        word = {"ACCEPT": "采纳", "REBUT": "反驳", "PENDING": "待定"}
        for r in detail["note_responses"]:
            L.append(f"- `{r['note_id']}` {word[r['response']]}：{r['why']}")
        L.append("")
    if detail.get("memory_candidates"):
        L += ["Memory 候选（等确认后生效）：" + "、".join(
            f"{m['body']['statement']['metric']}={m['body']['statement']['value']}" for m in detail["memory_candidates"]), ""]
    return "\n".join(L)
