"""复盘的规则：机会分类、事后信息、归因、观察。纯函数，输入是 facts.load 的结果。

这里只描述"发生了什么"，每条都带证据的 event_id；不下"应该怎么改"的结论（那是阶段 6B 的事）。
分组键来自配置，这个模块不认识任何策略专有的字段。
"""
from __future__ import annotations

from collections import Counter

RULES_VERSION = "review-rules-v1"
CATEGORIES = ("DATA_ENGINE_ERROR", "RULE_EXECUTION_DEVIATION", "LOSS_BY_RULE", "PROFIT_BY_RULE",
              "OUT_OF_RULE_OBSERVATION")
ENGINE_FLAGS = ("UNPRICEABLE", "UNABLE_TO_FLATTEN", "INVALID_AFTER_FILL")
MAJOR_AMBIGUITY = ("AMBIGUOUS_BAR", "GAP_THROUGH_STOP")
MINOR_AMBIGUITY = ("AMBIGUOUS_ENTRY", "AMBIGUOUS_PATH")
# 机制标签 -> (轨道, 说明模板)
MECHANISMS = {
    "TWO_STOPS_HALT": ("STRATEGY", "当日两次止损后停手，之后的信号被拦截"),
    "FILTERED_SETUP": ("STRATEGY", "信号被规则过滤，没有挂单"),
    "BLOCKED_SETUP": ("STRATEGY", "信号成立但没有挂单"),
    "EXPIRED_THEN_TRIGGERED": ("STRATEGY", "挂单到期未成交，之后当日价格越过了触发价"),
    "STOPPED_THEN_TARGET": ("STRATEGY", "止损出场后，当日价格到过原目标"),
    "AMBIGUOUS_FILL": ("STRATEGY", "成交带歧义标记"),
    "FORCED_FLAT": ("STRATEGY", "持仓到截止强平"),
    "DATA_QUALITY": ("DATA_ENGINE", "当日数据有问题"),
    "ENGINE_ANOMALY": ("DATA_ENGINE", "引擎出现异常标记"),
    "WATCH_ALERT_UNRESOLVED": ("MANUAL", "盘中提醒到收盘仍未处理"),
    "WATCH_ALERT_REPEATED": ("MANUAL", "同一规则在当日被重复触发"),
}


def _px(ticks, tick_size: str) -> str:
    from decimal import Decimal
    return "—" if ticks is None else str(Decimal(ticks) * Decimal(tick_size))


def opportunities(f: dict) -> dict:
    """规则允许的机会（生成了挂单）和被拦截的机会（信号被过滤，或成立但没有挂单）。"""
    orders = {o["order_id"]: o for o in f["orders"]}
    allowed, blocked = [], []
    for e in f["events"]:
        m = e["metadata"]
        base = {"event_id": e["event_id"], "as_of_utc": e["as_of_utc"], "setup": m.get("setup"), "label": m.get("label"),
                "side": m.get("side")}
        if e["order_id"]:
            o = orders.get(e["order_id"]) or {}
            allowed.append({**base, "order_id": e["order_id"], "order_status": o.get("status"),
                            "order_end_reason": o.get("end_reason")})
        elif e["signal_status"] == "SETUP_VALID":
            blocked.append({**base, "kind": "BLOCKED", "reason": e["block_reason"] or "UNKNOWN"})
        elif e["skip_reason"] and m.get("label"):       # 有形态候选、被过滤器或纪律拦下
            blocked.append({**base, "kind": "FILTERED", "reason": e["skip_reason"]})
    return {"allowed": allowed, "blocked": blocked}


def hindsight(f: dict) -> list[dict]:
    """事后才知道的事。只看当日 RTH 收盘之前的行情，不依赖次日。"""
    out = []
    bars = f["bars"]
    for o in f["orders"]:
        if o["status"] != "EXPIRED" or o["trigger_ticks"] is None or o["expires_at_utc"] is None:
            continue
        up = o["side"] == "LONG"
        hit = next((b for b in bars if b[0] >= o["expires_at_utc"]
                    and ((b[2] >= o["trigger_ticks"]) if up else (b[3] <= o["trigger_ticks"]))), None)
        if hit:
            out.append({"mechanism": "EXPIRED_THEN_TRIGGERED", "order_id": o["order_id"], "setup_id": o["setup_id"],
                        "at_utc": hit[0], "seconds_after_expiry": hit[0] - o["expires_at_utc"],
                        "trigger_ticks": o["trigger_ticks"]})
    for t in f["trades"]:
        if t["exit_reason"] == "STOP" and t.get("target_ticks") is not None:
            up = t["side"] == "LONG"
            hit = next((b for b in bars if b[0] >= t["exit_time_utc"]
                        and ((b[2] >= t["target_ticks"]) if up else (b[3] <= t["target_ticks"]))), None)
            if hit:
                out.append({"mechanism": "STOPPED_THEN_TARGET", "trade_id": t["trade_id"], "setup_id": t["setup_id"],
                            "at_utc": hit[0], "seconds_after_exit": hit[0] - t["exit_time_utc"],
                            "target_ticks": t["target_ticks"]})
    return out


def _evid(t: dict) -> str:
    return t.get("event_id") or t["trade_id"]


def attribute(f: dict) -> list[dict]:
    """每个结果归入且只归入一类。顺序：数据或引擎异常 → 执行偏差 → 证据不足 → 按盈亏。"""
    out = []
    for e in f["events"]:
        if e["signal_status"] == "DATA_ERROR":
            out.append({"event_id": e["event_id"], "category": "DATA_ENGINE_ERROR", "track": "DATA_ENGINE",
                        "note": "数据校验没有通过，不产生信号"})
    for t in f["trades"]:
        bad = [x for x in t["flags"] if x in ENGINE_FLAGS]
        if bad:
            cat, note = "DATA_ENGINE_ERROR", "引擎异常标记：" + "、".join(bad)
        elif t["net_usd"] > 0:
            cat, note = "PROFIT_BY_RULE", f"纸面交易按规则执行，{t['exit_reason']} 出场，净 {t['net_usd']} 美元"
        else:
            cat, note = "LOSS_BY_RULE", f"纸面交易按规则执行，{t['exit_reason']} 出场，净 {t['net_usd']} 美元"
        out.append({"event_id": _evid(t), "category": cat, "track": "STRATEGY", "note": note, "trade_id": t["trade_id"]})
    man = f.get("manual")
    if man:
        by_trade = Counter()
        for d in man["discipline_events"]:
            if d["plan_adherence"] == "VIOLATION" or d["type"] in ("UNPLANNED", "RETRY_WITHOUT_NEW_SETUP",
                                                                   "COUNTERTREND_BEFORE_CONFIRMATION"):
                by_trade[d["trade_id"]] += 1
        open_dev = {}                                  # 开仓时的偏差（无计划、无新形态重试）记在这笔仓位的平仓结果上
        for t in man["trades"]:
            if t["quarantined"]:
                continue
            if t["effect"] == "OPEN":
                if by_trade[t["trade_id"]]:
                    open_dev[(t["contract_id"], t["side"])] = t["trade_id"]
                continue
            from decimal import Decimal
            pnl = Decimal(t["realized_usd"] or "0")
            dev_open = open_dev.pop((t["contract_id"], t["side"]), None)
            if t["plan_adherence"] == "VIOLATION" or by_trade[t["trade_id"]] or dev_open:
                cat = "RULE_EXECUTION_DEVIATION"       # 盈利的违规仍是违规
                note = f"执行偏离计划或没有事前计划，已实现 {pnl} 美元"
            elif (t["plan_adherence"] or "UNKNOWN") == "UNKNOWN":
                cat, note = "OUT_OF_RULE_OBSERVATION", f"证据不足，无法判定是否按计划，已实现 {pnl} 美元"
            elif pnl > 0:
                cat, note = "PROFIT_BY_RULE", f"按计划执行，已实现 {pnl} 美元"
            else:
                cat, note = "LOSS_BY_RULE", f"按计划执行（含按计划止损），已实现 {pnl} 美元"
            out.append({"event_id": t["trade_id"], "category": cat, "track": "MANUAL", "note": note,
                        "trade_id": t["trade_id"]})
    return out


def observations(f: dict, opp: dict, hind: list[dict]) -> list[dict]:
    """观察：固定的一组机制标签，各带证据。同一机制一个交易日一条，证据合并。"""
    found: dict[str, dict] = {}

    def add(mech, evidence, **detail):
        o = found.setdefault(mech, {"mechanism": mech, "track": MECHANISMS[mech][0], "evidence": [], "items": []})
        if evidence and evidence not in o["evidence"]:
            o["evidence"].append(evidence)
        o["items"].append(detail)

    for b in opp["blocked"]:
        if b["reason"] == "TWO_STOPS_HALT":
            add("TWO_STOPS_HALT", b["event_id"], label=b["label"])
        elif b["kind"] == "FILTERED":
            add("FILTERED_SETUP", b["event_id"], reason=b["reason"], label=b["label"])
        else:
            add("BLOCKED_SETUP", b["event_id"], reason=b["reason"], label=b["label"])
    by_setup = {a_["order_id"]: a_["event_id"] for a_ in opp["allowed"]}
    ev_of_trade = {t["trade_id"]: _evid(t) for t in f["trades"]}
    for h in hind:
        ev = by_setup.get(h.get("order_id")) or ev_of_trade.get(h.get("trade_id"))
        add(h["mechanism"], ev, **{k: v for k, v in h.items() if k != "mechanism"})
    for t in f["trades"]:
        amb = [x for x in t["flags"] if x in MAJOR_AMBIGUITY + MINOR_AMBIGUITY]
        if amb:
            add("AMBIGUOUS_FILL", _evid(t), flags=amb, trade_id=t["trade_id"])
        if t["exit_reason"] == "FORCED_FLAT":
            add("FORCED_FLAT", _evid(t), trade_id=t["trade_id"])
        if any(x in ENGINE_FLAGS for x in t["flags"]):
            add("ENGINE_ANOMALY", _evid(t), flags=[x for x in t["flags"] if x in ENGINE_FLAGS], trade_id=t["trade_id"])
    for e in f["events"]:
        if e["signal_status"] == "DATA_ERROR":
            add("DATA_QUALITY", e["event_id"], problems=[p.get("category") for p in e["problems"]])
    day = f["day"]
    if day is not None and (not day["complete"] or day["degraded"]):
        first = f["events"][0]["event_id"] if f["events"] else None
        add("DATA_QUALITY", first, complete=bool(day["complete"]), degraded=bool(day["degraded"]), reason=day["reason"])
    man = f.get("manual")
    if man:
        for d in man["discipline_events"]:
            mech = "DISCIPLINE_" + d["type"]
            found.setdefault(mech, {"mechanism": mech, "track": "MANUAL", "evidence": [], "items": []})
            found[mech]["evidence"].append(d["event_id"])
            found[mech]["items"].append({"trade_id": d["trade_id"], "plan_adherence": d["plan_adherence"]})
        per_rule = Counter(a_["rule"] for a_ in man["alerts"])
        for a_ in man["alerts"]:
            if a_["resolved_at_utc"] is None or a_["resolved_at_utc"] > f["rth_close_utc"]:
                add("WATCH_ALERT_UNRESOLVED", a_["alert_id"], rule=a_["rule"])
            if per_rule[a_["rule"]] > 1:
                add("WATCH_ALERT_REPEATED", a_["alert_id"], rule=a_["rule"], count=per_rule[a_["rule"]])
    return [found[k] for k in sorted(found)]


def describe(obs: dict) -> str:
    base = MECHANISMS.get(obs["mechanism"], (None, None))[1]
    if base is None:                                   # 人工轨的纪律事件：标签就是事件类型
        base = "纪律事件 " + obs["mechanism"].removeprefix("DISCIPLINE_")
    reasons = Counter(str(i.get("reason")) for i in obs["items"] if i.get("reason"))
    extra = "（" + "、".join(f"{k}×{v}" for k, v in sorted(reasons.items())) + "）" if reasons else ""
    return f"{obs['mechanism']}：{base}{extra}，{len(obs['items'])} 次"


# ---------------------------------------------------------------- 对备注的回应
PREFERENCE_KEYS = {"report_max_lines": (1, 200), "watch_poll_interval_seconds": (10, 3600)}


def respond_to_notes(f: dict, plan_was_active) -> tuple[list[dict], list[dict]]:
    """逐条回应备注。规则只能核对两种格式，其余一律 PENDING：Python 不读懂自由文本。

    返回 (回应, Memory 候选)。plan_was_active(plan_id, at_utc) 用来核对"更正: plan_link=…"。
    """
    man = f.get("manual")
    if not man:
        return [], []
    trades = {t["trade_id"]: t for t in man["trades"]}
    responses, memory = [], []
    for n in man["notes"]:
        body = n["body"].strip()
        r = {"note_id": n["note_id"], "response": "PENDING", "evidence_event_ids": [n["event_id"]] if n["event_id"] else [],
             "why": "内容不属于规则可以核对的格式，留待人工判断"}
        key, _, val = body.partition(":") if ":" in body else body.partition("：")
        key, val = key.strip(), val.strip()
        if key == "偏好" and "=" in val:
            k, _, v = val.partition("=")
            k, v = k.strip(), v.strip()
            lim = PREFERENCE_KEYS.get(k)
            if lim is None:
                r.update(response="REBUT", why=f"{k} 不在可登记的偏好键里（允许：{'、'.join(sorted(PREFERENCE_KEYS))}）")
            elif not v.isdigit() or not lim[0] <= int(v) <= lim[1]:
                r.update(response="REBUT", why=f"{k} 的值应为 {lim[0]} 到 {lim[1]} 的整数")
            else:
                r.update(response="ACCEPT", why="明确表达的偏好，已生成 Memory 候选，等确认后生效")
                memory.append({"note_id": n["note_id"], "body": {
                    "kind": "user_preference", "scope": "global",
                    "statement": {"subject": "user_preference", "condition": None, "metric": k, "value": int(v),
                                  "unit": None, "sample_n": None, "period": None, "source": "trader_note"},
                    "source_event_ids": [n["note_id"]], "valid_until": None}})
        elif key == "更正" and val.startswith("plan_link="):
            pid = val.split("=", 1)[1].strip()
            t = trades.get(n["event_id"] or "")
            if t is None:
                r.update(why="这条更正没有关联到当日的成交")
            elif plan_was_active(pid, t["executed_at_utc"]):
                r.update(response="ACCEPT", why=f"计划 {pid} 在该成交之前已激活；更正已登记，账本不自动改写")
            else:
                r.update(response="REBUT", why=f"计划 {pid} 不存在，或在该成交时并未处于已激活状态")
        responses.append(r)
    return responses, memory
