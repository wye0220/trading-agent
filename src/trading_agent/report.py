"""数据可得性报告。对相同的库内容和配置，除生成时间外内容完全相同。"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

from . import contracts, sessions
from .config import Settings
from .ingest import HOLDOUT_NOT_SET

CAPSULE_DAYS = 20
CONFIRM_DAYS = 40
WARMUP_RTH_DAYS = 10
WARMUP_HOURLY = 10
GENERATED_PREFIX = "生成时间："


def _segments(covered: list[str]) -> list[list[str]]:
    """把有来源覆盖的交易日切成连续区段（中间隔着无覆盖的交易日就断开）。"""
    segs: list[list[str]] = []
    prev = None
    for d in covered:
        if prev is not None and sessions.next_trading_day(date.fromisoformat(prev)).isoformat() == d:
            segs[-1].append(d)
        else:
            segs.append([d])
        prev = d
    return segs


def collect(settings: Settings, con: sqlite3.Connection, cutoff: date | None) -> dict:
    """汇总报告所需的全部数字（供 write 和测试使用）。只统计非合成数据。"""
    out: dict = {"cutoff": cutoff.isoformat() if cutoff else None, "flags": []}
    real = con.execute("SELECT COUNT(*) FROM bars WHERE fixture=0 AND derived=0").fetchone()[0]
    fixt = con.execute("SELECT COUNT(*) FROM bars WHERE fixture=1 AND derived=0").fetchone()[0]
    out["real_bars"], out["fixture_bars"] = real, fixt
    if not real:
        out["flags"].append("NO_REAL_DATA")
    if cutoff is None:
        out["flags"].append(HOLDOUT_NOT_SET)

    # ---- 按来源
    out["sources"] = [dict(r) for r in con.execute(
        "SELECT source, timeframe, COUNT(*) bars, MIN(trading_day) first_day, MAX(trading_day) last_day,"
        " COUNT(DISTINCT trading_day) days, SUM(available_at_estimated) estimated, SUM(revision>1) revised"
        " FROM bars WHERE fixture=0 AND derived=0 GROUP BY source, timeframe ORDER BY source, timeframe")]

    roots = [r[0] for r in con.execute("SELECT DISTINCT instrument_root FROM trading_days WHERE fixture=0 ORDER BY 1")]
    out["roots"] = {}
    for root in roots:
        inst = settings.instrument(root)
        rows = con.execute("SELECT * FROM trading_days WHERE instrument_root=? AND fixture=0 ORDER BY trading_day",
                           (root,)).fetchall()
        info: dict = {}
        covered = [r["trading_day"] for r in rows if r["source"]]
        by_day = {r["trading_day"]: r for r in rows}
        info["first_day"], info["last_day"] = (covered[0], covered[-1]) if covered else (None, None)
        info["days_by_source"] = dict(con.execute(
            "SELECT source, COUNT(*) FROM trading_days WHERE instrument_root=? AND fixture=0 AND source IS NOT NULL"
            " GROUP BY source", (root,)).fetchall())

        # 无来源覆盖的区段（首末覆盖日之间）
        uncovered = []
        if covered:
            cal = [d.isoformat() for d in sessions.trading_days(date.fromisoformat(covered[0]),
                                                                date.fromisoformat(covered[-1]))]
            cs = set(covered)
            run = []
            for d in cal:
                if d not in cs:
                    run.append(d)
                elif run:
                    uncovered.append({"from": run[0], "to": run[-1], "trading_days": len(run)})
                    run = []
        info["uncovered"] = uncovered

        # 多来源重叠
        ov_days, ov_bars, ov_diff = 0, 0, 0
        for r in con.execute(
                "SELECT trading_day, contract_id FROM trading_days t WHERE instrument_root=? AND fixture=0 AND"
                " (SELECT COUNT(DISTINCT source) FROM bars b WHERE b.contract_id=t.contract_id AND b.timeframe='5m'"
                "  AND b.trading_day=t.trading_day AND b.session_type='RTH') > 1", (root,)).fetchall():
            ov_days += 1
            per = defaultdict(dict)
            for b in con.execute(
                    "SELECT source, bar_start_utc, open, high, low, close FROM bars b WHERE contract_id=? AND"
                    " timeframe='5m' AND trading_day=? AND session_type='RTH' AND revision=(SELECT MAX(revision)"
                    " FROM bars x WHERE x.source=b.source AND x.contract_id=b.contract_id AND x.timeframe=b.timeframe"
                    " AND x.bar_start_utc=b.bar_start_utc)", (r["contract_id"], r["trading_day"])):
                per[b["bar_start_utc"]][b["source"]] = tuple(b)[2:]
            for start, m in per.items():
                if len(m) > 1:
                    ov_bars += 1
                    ov_diff += len(set(m.values())) > 1
        info["overlap"] = {"trading_days": ov_days, "bars_compared": ov_bars, "bars_different": ov_diff}

        # 质量
        info["degraded_days"] = [r["trading_day"] for r in rows if r["degraded"]]
        info["gap_days"] = [(r["trading_day"], r["gap_count"], r["rth_expected"] - r["rth_present"])
                            for r in rows if r["source"] and r["gap_count"]]
        info["wrong_contract_days"] = [r["trading_day"] for r in rows if r["wrong_contract"]]
        info["complete_days"] = sum(1 for r in rows if r["complete"])
        info["early_close_days"] = sum(1 for r in rows if r["early_close"] and r["source"])
        info["thin_bars"] = sum(r["thin_count"] for r in rows)

        # 预热与窗口
        hourly = dict(con.execute(
            "SELECT trading_day, COUNT(DISTINCT bar_start_utc) FROM bars WHERE instrument_root=? AND timeframe='1h'"
            " AND fixture=0 GROUP BY trading_day", (root,)).fetchall())
        segs = []
        tot_caps = tot_conf = 0
        for seg in _segments(covered):
            complete_before, hourly_before, warm_from, valid = 0, 0, None, 0
            for d in seg:
                r = by_day[d]
                if warm_from is None and complete_before >= WARMUP_RTH_DAYS and hourly_before >= WARMUP_HOURLY:
                    warm_from = d
                if warm_from is not None and r["complete"] and not r["degraded"]:
                    valid += 1
                complete_before += r["complete"]
                hourly_before += hourly.get(d, 0)
            caps, conf = valid // CAPSULE_DAYS, valid // CONFIRM_DAYS
            tot_caps += caps
            tot_conf += conf
            segs.append({"from": seg[0], "to": seg[-1], "trading_days": len(seg), "warm_from": warm_from,
                         "warmup_short_days": seg.index(warm_from) if warm_from else len(seg),
                         "valid_days": valid, "capsules_20": caps, "confirm_40": conf})
        info["segments"] = segs
        info["capsules_20"], info["confirm_40"] = tot_caps, tot_conf

        # 按实际合约
        per_contract = []
        active_stats = {r[0]: r for r in con.execute(
            "SELECT contract_id, COUNT(*) days, SUM(complete) complete, SUM(gap_count) gaps, SUM(degraded) degraded,"
            " MIN(trading_day) a, MAX(trading_day) b FROM trading_days WHERE instrument_root=? AND fixture=0"
            " AND source IS NOT NULL GROUP BY contract_id", (root,))}
        for r in con.execute(
                "SELECT contract_id, MIN(bar_start_utc) first_bar, MAX(bar_end_utc) last_bar, COUNT(*) bars,"
                " COUNT(DISTINCT CASE WHEN session_type='RTH' THEN trading_day END) rth_days,"
                " COUNT(DISTINCT CASE WHEN session_type='ETH' THEN trading_day END) eth_days,"
                " SUM(available_at_estimated) est, SUM(revision>1) revised"
                " FROM bars WHERE instrument_root=? AND fixture=0 AND derived=0 GROUP BY contract_id"
                " ORDER BY MIN(bar_start_utc), contract_id", (root,)):
            a = active_stats.get(r["contract_id"])
            per_contract.append({
                "contract_id": r["contract_id"], "first_bar": sessions.iso(r["first_bar"]),
                "last_bar": sessions.iso(r["last_bar"]), "bars": r["bars"], "rth_days": r["rth_days"],
                "eth_days": r["eth_days"], "estimated_pct": round(100 * r["est"] / r["bars"], 1),
                "revised": r["revised"],
                "active_from": a["a"] if a else None, "active_to": a["b"] if a else None,
                "active_days": a["days"] if a else 0, "active_complete": a["complete"] if a else 0,
                "active_gaps": a["gaps"] if a else 0, "active_degraded": a["degraded"] if a else 0})
        info["contracts"] = per_contract

        # 换月核对：规则日期 vs 成交量交叉日
        rolls = []
        if covered:
            vol = defaultdict(dict)
            for r in con.execute(
                    "SELECT contract_id, trading_day, SUM(volume) v FROM bars b WHERE instrument_root=? AND fixture=0"
                    " AND derived=0 AND session_type='RTH' AND revision=(SELECT MAX(revision) FROM bars x WHERE"
                    " x.source=b.source AND x.contract_id=b.contract_id AND x.timeframe=b.timeframe AND"
                    " x.bar_start_utc=b.bar_start_utc) AND source=(SELECT source FROM trading_days t WHERE"
                    " t.instrument_root=b.instrument_root AND t.trading_day=b.trading_day)"
                    " GROUP BY contract_id, trading_day", (root,)):
                vol[r["contract_id"]][r["trading_day"]] = r["v"]
            for rd, old, new in contracts.roll_schedule(inst, date.fromisoformat(covered[0]),
                                                        date.fromisoformat(covered[-1]), settings.roll_rules):
                rule_day = rd if sessions.is_trading_day(rd) else sessions.next_trading_day(rd)
                cross = None
                both = sorted(set(vol.get(old, {})) & set(vol.get(new, {})))
                near = [d for d in both if abs((date.fromisoformat(d) - rd).days) <= 45]
                for d in near:
                    if vol[new][d] > vol[old][d]:
                        cross = d
                        break
                diff = None
                if cross:
                    a, b = sorted((rule_day, date.fromisoformat(cross)))
                    n = len(sessions.trading_days(a, b)) - 1
                    diff = n if date.fromisoformat(cross) >= rule_day else -n
                rolls.append({"rule_date": rule_day.isoformat(), "old": old, "new": new,
                              "rule_version": inst.roll_rule_version, "volume_cross": cross,
                              "cross_minus_rule_days": diff, "days_compared": len(near)})
        info["rolls"] = rolls
        out["roots"][root] = info

    # ---- 截止日之后：只报覆盖
    out["withheld"] = [dict(r) for r in con.execute(
        "SELECT source, contract_id, timeframe, COUNT(*) days, MIN(trading_day) first_day, MAX(trading_day) last_day,"
        " SUM(bar_count) bars FROM withheld_coverage GROUP BY source, contract_id, timeframe ORDER BY 1, 5, 2")]
    wd = []
    for root in settings.instruments:
        inst = settings.instrument(root)
        for r in con.execute(
                "SELECT trading_day, source, contract_id, SUM(bar_count) bars, MAX(rth_slots_present) slots"
                " FROM withheld_coverage WHERE instrument_root=? GROUP BY trading_day, source, contract_id"
                " ORDER BY trading_day", (root,)):
            d = date.fromisoformat(r["trading_day"])
            if not sessions.is_trading_day(d) or contracts.active_contract(inst, d, settings.roll_rules) != r["contract_id"]:
                continue
            exp = len(sessions.expected_rth_slots(d))
            q = con.execute("SELECT condition FROM day_quality WHERE source=? AND day=?",
                            (r["source"], r["trading_day"])).fetchone()
            wd.append({"root": root, "trading_day": r["trading_day"], "source": r["source"], "bars": r["bars"],
                       "rth_slots_missing": exp - r["slots"], "degraded": bool(q) and q[0] != "available"})
    out["withheld_days"] = wd

    out["anomalies"] = dict(con.execute(
        "SELECT category, COUNT(*) FROM data_anomalies GROUP BY category ORDER BY category").fetchall())
    out["files"] = [dict(r) for r in con.execute(
        "SELECT path, kind, data_schema, status, records, size, sha256 FROM ingest_files ORDER BY path")]
    return out


def _table(headers: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["（无）", ""]
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    out += ["| " + " | ".join("—" if v is None else str(v) for v in r) + " |" for r in rows]
    return out + [""]


def render(settings: Settings, data: dict, generated_at: int | None = None) -> str:
    L: list[str] = ["# 数据可得性报告", ""]
    L.append(GENERATED_PREFIX + sessions.iso(int(time.time()) if generated_at is None else generated_at))
    L.append("")
    for f in data["flags"]:
        if f == "NO_REAL_DATA":
            L.append("> **NO_REAL_DATA**：库中没有真实行情，只有合成样本。以下数字不能作为任何策略证据。")
        elif f == HOLDOUT_NOT_SET:
            L.append("> **HOLDOUT_BOUNDARY_NOT_SET**：没有配置开发可见截止日（`dev_visible_through`），全部数据都已进入运行库。"
                     "阶段 5 分配盲测胶囊之前必须先定下日期清单。")
    if data["cutoff"]:
        L.append(f"> 开发可见截止日：**{data['cutoff']}**。晚于它的数据没有进入运行库，下文只报覆盖。")
    L += ["", f"- 日历：`{sessions.CALENDAR_VERSION}`",
          f"- 历史数据的 `available_at` 估计延迟：{settings.available_at_delay_seconds} 秒",
          f"- 来源优先级：{' > '.join(settings.source_priority)}",
          f"- 原始 K 线：真实 {data['real_bars']:,} 根，合成（FIXTURE）{data['fixture_bars']:,} 根", ""]

    if "NO_REAL_DATA" in data["flags"]:
        L += ["## 真实行情还缺什么", "",
              "- **字段**：实际合约的开高低收量、起止时刻、来源；有取数时刻最好，没有则按估计延迟处理。",
              "- **频率**：5 分钟线（或可聚合成 5 分钟的 1 分钟线）。",
              "- **时段**：RTH 和 ETH 都要。ONH、ONL 和 1 小时线预热需要夜盘。",
              "- **覆盖**：至少 10 个完整 RTH 日用于 ADR 预热，另加开发、验证、20 日盲筛和 40 日确认窗口。", "",
              "## 可分配窗口", "", "- 可封存的 20 日胶囊：**0**", "- 连续 40 日确认段：**0**", ""]

    L += ["## 按来源汇总", ""]
    L += _table(["来源", "原始周期", "K 线数", "首个交易日", "末个交易日", "交易日数", "available_at 为估计", "revision>1"],
                [[s["source"], s["timeframe"], f"{s['bars']:,}", s["first_day"], s["last_day"], s["days"],
                  f"{100 * s['estimated'] / s['bars']:.1f}%", s["revised"]] for s in data["sources"]])

    for root, info in data["roots"].items():
        inst = settings.instrument(root)
        L += [f"## {root}", "",
              f"- 有来源覆盖的交易日：{info['first_day']} 至 {info['last_day']}",
              "- 各来源被选用的交易日数：" + ("，".join(f"{k} {v}" for k, v in sorted(info["days_by_source"].items())) or "无"),
              f"- 完整有效的 RTH 日：{info['complete_days']}（其中提前收盘 {info['early_close_days']} 天）",
              f"- 降级日：{len(info['degraded_days'])}，有非预期缺口的交易日：{len(info['gap_days'])}，"
              f"合约错误的交易日：{len(info['wrong_contract_days'])}",
              f"- RTH 内构成偏少的 5 分钟线（THIN_BAR）：{info['thin_bars']}", ""]

        L += ["### 来源衔接", ""]
        if info["uncovered"]:
            L.append("首末覆盖日之间，以下区段**没有任何来源覆盖**：")
            L.append("")
            L += _table(["起", "止", "交易日数"], [[u["from"], u["to"], u["trading_days"]] for u in info["uncovered"]])
        else:
            L += ["首末覆盖日之间没有无来源覆盖的区段。", ""]
        ov = info["overlap"]
        L += [f"多个来源同时覆盖的交易日：{ov['trading_days']} 天；比对 RTH 5 分钟线 {ov['bars_compared']} 根，"
              f"开高低收不一致 {ov['bars_different']} 根。", ""]

        L += ["### 可分配窗口", "",
              f"- 可封存的 20 日胶囊：**{info['capsules_20']}**",
              f"- 连续 40 日确认段：**{info['confirm_40']}**", "",
              "计数口径：只计完整有效的 RTH 日（无非预期缺口、非降级日），从每个连续覆盖区段预热充足的那天起算，"
              f"窗口不跨越无来源覆盖的区段。胶囊数 = 有效日 ÷ {CAPSULE_DAYS} 取整，确认段数 = 有效日 ÷ {CONFIRM_DAYS} 取整。"
              "开发期和验证窗要从这些天数里另外扣除（阶段 5）。", ""]
        L += ["### 预热覆盖", "",
              f"预热要求：之前至少 {WARMUP_RTH_DAYS} 个完整 RTH 日（ADR）和 {WARMUP_HOURLY} 根已完结 1 小时线。", ""]
        L += _table(["区段起", "区段止", "交易日数", "预热不足的天数", "首个预热充足的交易日", "有效日", "20 日胶囊", "40 日确认段"],
                    [[s["from"], s["to"], s["trading_days"], s["warmup_short_days"], s["warm_from"], s["valid_days"],
                      s["capsules_20"], s["confirm_40"]] for s in info["segments"]])

        L += ["### 换月核对", "",
              f"规则 `{inst.roll_rule_version}`：到期日（第三个星期五）前第 "
              f"{settings.roll_rules[inst.roll_rule_version]['days_before_expiry']} 个日历日所在交易日起换到下一季月。"
              "成交量交叉日 = 新合约 RTH 成交量首次超过旧合约的交易日。差值为交叉日减规则日（交易日数），"
              "负数表示流动性比规则更早转移。", ""]
        L += _table(["规则换月日", "旧合约", "新合约", "规则版本", "成交量交叉日", "差值（交易日）"],
                    [[r["rule_date"], r["old"], r["new"], r["rule_version"], r["volume_cross"],
                      r["cross_minus_rule_days"]] for r in info["rolls"]])

        L += ["### 降级日", ""]
        L += ([", ".join(info["degraded_days"]), ""] if info["degraded_days"] else ["（无）", ""])
        L += ["### 有非预期缺口的交易日", ""]
        L += _table(["交易日", "缺口段数", "缺失的 5 分钟线"], [list(g) for g in info["gap_days"]])
        if info["wrong_contract_days"]:
            L += ["### 合约错误的交易日", "", ", ".join(info["wrong_contract_days"]), ""]

        L += ["### 按实际合约", "",
              "「作为实际合约」各列只统计换月规则指定它为实际合约的交易日。", ""]
        L += _table(["合约", "首根（UTC）", "末根（UTC）", "原始 K 线", "RTH 有数据日", "ETH 有数据日", "估计占比", "revision>1",
                     "作为实际合约：起", "止", "交易日", "完整 RTH 日", "缺口段", "降级日"],
                    [[c["contract_id"], c["first_bar"], c["last_bar"], f"{c['bars']:,}", c["rth_days"], c["eth_days"],
                      f"{c['estimated_pct']}%", c["revised"], c["active_from"], c["active_to"], c["active_days"],
                      c["active_complete"], c["active_gaps"], c["active_degraded"]] for c in info["contracts"]])

    if data["withheld"] or data["cutoff"]:
        L += ["## 截止日之后（只报覆盖，不含价格）", ""]
        wd = data["withheld_days"]
        L += [f"- 实际合约有数据的交易日：{len(wd)}",
              f"- 其中 RTH 5 分钟格有缺失的交易日：{sum(1 for d in wd if d['rth_slots_missing'] > 0)}",
              f"- 其中降级日：{sum(1 for d in wd if d['degraded'])}", ""]
        L += _table(["来源", "合约", "周期", "交易日数", "首日", "末日", "K 线数"],
                    [[w["source"], w["contract_id"], w["timeframe"], w["days"], w["first_day"], w["last_day"],
                      f"{w['bars']:,}"] for w in data["withheld"]])

    L += ["## 异常汇总", ""]
    L += _table(["类别", "条数"], [[k, v] for k, v in data["anomalies"].items()])
    L += ["## 已处理的文件", ""]
    L += _table(["路径（相对数据根目录）", "类型", "schema", "状态", "记录数", "大小", "SHA-256"],
                [[f["path"], f["kind"], f["data_schema"], f["status"], f"{f['records']:,}", f"{f['size']:,}",
                  f["sha256"][:16] + "…"] for f in data["files"]])
    return "\n".join(L).rstrip() + "\n"


def write(settings: Settings, con: sqlite3.Connection, path: Path, cutoff: date | None) -> dict:
    data = collect(settings, con, cutoff)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(settings, data), encoding="utf-8")
    return data


def body(text: str) -> str:
    """去掉生成时间那一行，用于比较两份报告。"""
    return "\n".join(l for l in text.splitlines() if not l.startswith(GENERATED_PREFIX))
