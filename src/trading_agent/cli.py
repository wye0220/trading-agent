"""python -m trading_agent 命令行入口。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date

from . import db, sessions
from .config import DEFAULT_SCOPE, ConfigError, load_settings, parse_scope

# 子命令 -> 负责实现它的阶段。阶段 1 实现 ingest、collect 和最小 scan。
COMMANDS = {
    "ingest": 1, "collect": 1, "scan": 4,
    "plan": 4, "watch": 4, "replay": 5, "baseline": 5, "review": "6A", "evolve": "6B",
    "evaluate": 8, "shadow": 9, "release": 9, "rollback": 9, "freeze": 9, "monitor": 9,
}
IMPLEMENTED = {"ingest", "collect", "scan", "plan", "watch", "replay", "baseline"}
HELP = {
    "ingest": "扫描数据根目录，增量导入行情并生成数据可得性报告",
    "collect": "只读采集已收线的 K 线并入库。目前唯一的适配器是 Webull，而该账号没有期货行情，所以暂不可用",
    "scan": "数据校验后运行策略和纸面账本，输出 JSON 和中文短报",
    "plan": "人工交易前登记与预检（只给结论，不下单）", "watch": "盘中只读纪律提醒",
    "replay": "在一段日期上回放策略和纸面账本（与 scan 共用同一个引擎）",
    "baseline": "冻结基线：登记数据切分、回放两次校验一致、生成基线报告和功效测算",
    "review": "日复盘", "evolve": "聚合演化与候选台账", "evaluate": "独立评测",
    "shadow": "影子运行", "release": "发布", "rollback": "回滚", "freeze": "冻结", "monitor": "发布后监控",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m trading_agent",
                                description="trading-agent：行情只读、纸面模拟。没有任何实盘下单功能。")
    sub = p.add_subparsers(dest="command", metavar="<子命令>")
    for name, phase in COMMANDS.items():
        note = "" if name in IMPLEMENTED else f"（阶段 {phase}，尚未实现）"
        sp = sub.add_parser(name, help=HELP[name] + note)
        sp.add_argument("--scope", default=DEFAULT_SCOPE,
                        help=f"strategy_family:instrument_root:session_type:timeframe，默认 {DEFAULT_SCOPE}")
        sp.add_argument("--json", action="store_true", help="输出 JSON")
        if name == "ingest":
            sp.add_argument("--through", metavar="YYYY-MM-DD",
                            help="开发可见截止日：晚于它的 K 线不进运行库（覆盖 config/paths.yaml 的 dev_visible_through）")
            sp.add_argument("--no-report", action="store_true", help="不生成数据可得性报告")
            sp.add_argument("--trades", metavar="CSV", help="导入人工成交文件（不扫描行情目录）")
            sp.add_argument("--order-events", metavar="CSV", help="导入订单生命周期事件文件（建议与 --trades 一起给）")
            sp.add_argument("--rebuild", action="store_true",
                            help="清空派生数据和交易日状态后从原始 K 线重算（改了换月规则等配置后使用）")
        if name == "collect":
            sp.add_argument("--from-mcp", metavar="JSON",
                            help="读 Webull MCP 连接器 get_futures_bars 返回的 JSON 文件（文件里有几个已登记的品种就存几个）")
            sp.add_argument("--fetched-at", metavar="ISO8601", help="该文件的取数时刻（UTC）。缺省用文件的修改时间")
            sp.add_argument("--check-day", metavar="YYYY-MM-DD",
                            help="只检查该交易日 ES、NQ 的已存文件是否覆盖了全部 RTH 5 分钟线（不取数、不写数据）")
            sp.add_argument("--mcp-request", action="store_true",
                            help="只打印此刻该向 Webull MCP 连接器请求的合约代码和起始时刻（不取数、不写数据）")
        if name == "scan":
            sp.add_argument("--as-of", metavar="ISO8601", help="历史扫描的决策时点（UTC，或带时区）。缺省为盘中扫描")
            sp.add_argument("--allow-real-data", action="store_true",
                            help="历史扫描时允许在真实数据上运行策略（还需要已设置开发可见截止日）")
        if name == "plan":
            sp.add_argument("tokens", nargs="*", metavar="字段",
                            help="快捷语法：根代码 方向 入场 止损 目标 手数 理由…（止损、目标可写 -）")
            sp.add_argument("--account", help="账户标识（缺省取 config/manual.yaml 的 default_account_id）")
            sp.add_argument("--activate", metavar="PLAN_ID", help="激活一份已预检的计划（预留风险预算）")
            sp.add_argument("--cancel", metavar="PLAN_ID", help="取消一份计划")
            sp.add_argument("--list", action="store_true", help="列出计划")
        if name == "watch":
            sp.add_argument("--account", help="账户标识")
            sp.add_argument("--once", action="store_true", help="只评估一次就退出")
            sp.add_argument("--interval", type=int, metavar="秒", help="轮询间隔（缺省取 config/watch.yaml，常用 60 或 300）")
        if name == "replay":
            sp.add_argument("--from", dest="start", metavar="YYYY-MM-DD", help="回放起始交易日")
            sp.add_argument("--to", dest="end", metavar="YYYY-MM-DD", help="回放结束交易日")
            sp.add_argument("--run-id", help="运行标识（缺省自动生成）")
        if name in ("replay", "baseline"):
            sp.add_argument("--allow-real-data", action="store_true",
                            help="允许在真实数据上运行（还需要已设置开发可见截止日，且区间不晚于它）")
        if name == "baseline":
            sp.add_argument("--skip-power", action="store_true", help="只冻结基线和出基线报告，不跑扰动和功效测算")
            sp.add_argument("--jobs", type=int, default=1, metavar="N", help="并行回放扰动的进程数（缺省 1）")
        if name not in IMPLEMENTED:
            sp.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return p


def _emit(obj: dict, as_json: bool, text: str | None = None):
    if as_json or text is None:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(text)


def main(argv: list[str] | None = None, *, settings=None, adapter=None, registry=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    if args.command not in IMPLEMENTED:
        # 不加载配置、不打开数据库：未实现的子命令不读写任何业务数据。
        _emit({"status": "NOT_IMPLEMENTED", "command": args.command, "phase": str(COMMANDS[args.command]),
               "message": f"{args.command} 由阶段 {COMMANDS[args.command]} 实现"}, True)
        return 3

    try:
        settings = settings or load_settings()
        scope = parse_scope(args.scope, settings)
    except ConfigError as e:
        _emit({"status": "ERROR", "error": "CONFIG", "message": str(e)}, args.json, f"错误：{e}")
        return 2

    con = db.open_runtime(settings)
    try:
        if args.command == "ingest" and (args.trades or args.order_events):
            from .engine.config import load_risk
            from .manual import trades as trades_mod
            from .manual.config import load_manual
            out = {"ok": True, "results": []}
            try:
                if args.order_events:                 # 先导入订单事件：成交的退出类型要用到它
                    out["results"].append(trades_mod.import_order_events(settings, con, args.order_events))
                if args.trades:
                    out["results"].append(trades_mod.import_trades(
                        settings, con, args.trades, risk_cfg=load_risk(settings.project_root),
                        manual_cfg=load_manual(settings.project_root)))
            except (trades_mod.ImportError_, OSError) as e:
                _emit({"ok": False, "status": "ERROR", "error": "REJECTED", "message": str(e)}, args.json,
                      f"拒收：{e}")
                return 1
            text = "\n".join(
                f"{'订单事件' if r['kind'] == 'order_events' else '成交'}：导入 {r['imported']}，跳过 {r['skipped']}，"
                + (f"隔离 {r['quarantined']}，" if "quarantined" in r else "") + f"无法解析 {r['bad_rows']}"
                + (f"（{r['first_error']}）" if r.get("first_error") else "") for r in out["results"])
            _emit(out, args.json, text)
            return 0

        if args.command == "ingest":
            from .pipeline import run_ingest
            try:
                through = date.fromisoformat(args.through) if args.through else None
            except ValueError:
                _emit({"status": "ERROR", "error": "CONFIG", "message": f"--through 不是合法日期：{args.through}"},
                      args.json, f"错误：--through 不是合法日期：{args.through}")
                return 2
            db.open_release(settings).close()        # 只建表，不写 active 指针
            s = run_ingest(settings, con, through=through, write_report=not args.no_report, rebuild=args.rebuild)
            text = (f"导入完成（{s['run_id']}，{s['seconds']} 秒）：处理 {s['files_imported']} 个文件，跳过 {s['files_skipped']} 个，"
                    f"不支持 {s['files_unsupported']} 个，拒收 {s['files_rejected']} 个；新增 K 线 {s['bars_inserted']:,}，"
                    f"新 revision {s['revisions_added']:,}，跳过重复 {s['duplicates_skipped']:,}；"
                    f"丢弃价差代码 {s['spread_symbols_dropped']} 个、记录 {s['spread_records_dropped']:,} 条；"
                    f"截止日之后未入库 {s['withheld_after_cutoff']:,} 条"
                    + (f"，从运行库移出 {s['moved_after_cutoff']:,} 条" if s["moved_after_cutoff"] else "") + "。"
                    + (f"\n异常：{s['anomalies']}" if s["anomalies"] else "")
                    + ("\n标记：" + "、".join(s["flags"]) if s["flags"] else "")
                    + (f"\n报告：{s['report']}" if s.get("report") else ""))
            _emit(s, args.json, text)
            return 0

        if args.command == "replay":
            return _replay(args, settings, con, scope, registry)

        if args.command == "baseline":
            return _baseline(args, settings, con, scope, registry)

        if args.command == "collect" and args.check_day:
            import csv as _csv

            from . import contracts
            from .collect import csv_path
            try:
                day = date.fromisoformat(args.check_day)
            except ValueError:
                _emit({"ok": False, "error": "CONFIG", "message": f"不是合法日期：{args.check_day}"}, args.json,
                      f"错误：不是合法日期：{args.check_day}")
                return 2
            if not sessions.is_trading_day(day):
                _emit({"ok": True, "trading_day": args.check_day, "is_trading_day": False, "results": []}, args.json,
                      f"{args.check_day} 不是交易日，不需要数据。")
                return 0
            expected = {sessions.iso(s) for s in sessions.expected_rth_slots(day)}
            results = []
            for root in [r for r in settings.collect.get("mcp_roots", ["ES", "NQ"]) if r in settings.instruments]:
                cid = contracts.active_contract(settings.instrument(root), day, settings.roll_rules)
                path = csv_path(settings, root, cid, day.isoformat())
                have, rows = set(), 0
                if path.is_file():
                    with open(path, encoding="utf-8", newline="") as f:
                        for r in _csv.DictReader(f):
                            rows += 1
                            have.add(r["bar_start_utc"])
                missing = len(expected - have)
                results.append({"instrument_root": root, "contract_id": cid, "file": str(path), "rows": rows,
                                "rth_expected": len(expected), "rth_missing": missing, "ok": missing == 0})
            ok = all(r["ok"] for r in results)
            _emit({"ok": ok, "trading_day": args.check_day, "is_trading_day": True, "results": results}, args.json,
                  "\n".join(f"{r['instrument_root']} {r['contract_id']}：{r['rows']} 行，RTH 缺 {r['rth_missing']}/{r['rth_expected']} 根"
                            + ("" if r["ok"] else "  ← 不完整") for r in results))
            return 0 if ok else 1

        if args.command == "collect" and args.mcp_request:
            import time as _time

            from . import contracts
            now = int(_time.time())
            day = sessions.trading_day_at(now)
            back = day
            for _ in range(settings.collect["lookback_trading_days"] - 1):
                back = sessions.prev_trading_day(back)
            roots = [r for r in settings.collect.get("mcp_roots", ["ES", "NQ"]) if r in settings.instruments]
            symbols = [contracts.short_symbol(contracts.active_contract(settings.instrument(r), day, settings.roll_rules))
                       for r in roots]
            start = sessions.eth_bounds(back)[0]
            out = {"ok": True, "tool": "get_futures_bars", "symbols": ",".join(symbols), "category": "US_FUTURES",
                   "timespan": "M5", "start_time": sessions.iso(start).replace("Z", ".000Z"),
                   "count": min(1200, (now - start) // 300 + 2), "trading_day": day.isoformat()}
            _emit(out, args.json, f"symbols={out['symbols']} category=US_FUTURES timespan=M5 "
                                  f"start_time={out['start_time']} count={out['count']}（交易日 {out['trading_day']}）")
            return 0

        if args.command == "collect" and args.from_mcp:
            return _collect_mcp(args, settings, con, scope)

        if args.command == "collect":
            from .collect import run_collect
            if adapter is None:
                from .sources.webull import WebullBars
                adapter = WebullBars(project_root=settings.project_root)
            s = run_collect(settings, con, scope, adapter)
            if s["ok"]:
                text = (f"采集完成（{s['run_id']}）：{s['contract_id']} {s['timeframe']}，取到 {s['rows_fetched']} 根，"
                        f"写入 {s['rows_written']} 行（新 {s['new_bars']}，修订 {s['revisions']}），"
                        f"未收线跳过 {s['skipped_unclosed']}，已有跳过 {s['skipped_existing']}。")
            else:
                text = f"采集失败：{s['message']}。没有写入任何数据。"
                if s["error"] == "MARKET_DATA_NOT_SUBSCRIBED":
                    text += "\n说明：Webull 账号没有美国期货行情，实时来源暂缓接入（见 docs/data-contract.md）。"
            _emit(s, args.json, text)
            return 0 if s["ok"] else 1

        if args.command == "scan":
            from . import scanner
            from .strategies.registry import default_registry
            try:
                as_of = sessions.parse_utc(args.as_of) if args.as_of else None
            except ValueError:
                _emit({"status": "ERROR", "error": "CONFIG", "message": f"--as-of 不是合法时刻：{args.as_of}"},
                      args.json, f"错误：--as-of 不是合法时刻：{args.as_of}")
                return 2
            strategy = (registry or default_registry()).get(scope.strategy_family)
            if as_of is None:
                r = scanner.live_scan(settings, con, scope, strategy)
            else:
                r = scanner.historical_scan(settings, con, scope, strategy, as_of,
                                            allow_real_data=args.allow_real_data)
            _emit(r, args.json, r["report"])
            return 0

        if args.command == "plan":
            return _plan(args, settings, con, scope, registry)

        if args.command == "watch":
            return _watch(args, settings, con, scope, registry, adapter)
    finally:
        con.close()
    return 2


def _collect_mcp(args, settings, con, scope) -> int:
    import os
    from datetime import datetime, timezone

    from .collect import run_collect
    from .config import Scope
    from .sources.webull import FetchError
    from .sources.webull_mcp import McpDump
    try:
        now = sessions.parse_utc(args.fetched_at) if args.fetched_at else int(os.path.getmtime(args.from_mcp))
        dump = McpDump(args.from_mcp, datetime.fromtimestamp(now, timezone.utc).date())
    except (FetchError, OSError, ValueError) as e:
        _emit({"ok": False, "status": "ERROR", "error": "BAD_DUMP", "message": str(e)}, args.json, f"拒收：{e}")
        return 1
    results, lines = [], []
    for root, cid in sorted(dump.roots().items()):
        if root not in settings.instruments:
            results.append({"ok": False, "instrument_root": root, "error": "UNREGISTERED_ROOT", "rows_written": 0})
            lines.append(f"{root}：品种没有登记，跳过。")
            continue
        sc = Scope(scope.strategy_family, root, scope.session_type, scope.timeframe)
        s = run_collect(settings, con, sc, dump, now=now)
        results.append({"instrument_root": root, **s})
        lines.append(
            f"{root}：{s['contract_id']} 取到 {s['rows_fetched']} 根，写入 {s['rows_written']} 行"
            f"（新 {s['new_bars']}，修订 {s['revisions']}），未收线跳过 {s['skipped_unclosed']}，"
            f"已有跳过 {s['skipped_existing']}。" if s["ok"] else f"{root}：失败，{s['message']}。没有写入。")
    ok = bool(results) and all(r["ok"] for r in results)
    _emit({"ok": ok, "fetched_at_utc": sessions.iso(now), "results": results}, args.json,
          "\n".join(lines) or "文件里没有任何合约。")
    return 0 if ok else 1


def _registry(registry):
    if registry is not None:
        return registry
    from .strategies.registry import default_registry
    return default_registry()


def _replay(args, settings, con, scope, registry) -> int:
    from .engine import runner
    try:
        if not args.start or not args.end:
            raise ValueError("需要 --from 和 --to")
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        if end < start:
            raise ValueError("--to 不能早于 --from")
    except ValueError as e:
        _emit({"ok": False, "status": "ERROR", "error": "CONFIG", "message": str(e)}, args.json, f"错误：{e}")
        return 2
    try:
        strategy = _registry(registry).get(scope.strategy_family)
        r = runner.replay(settings, con, scope, strategy, start, end, run_id=args.run_id,
                          allow_real_data=args.allow_real_data)
    except runner.ReplayRefused as e:
        _emit({"ok": False, "status": "REFUSED", "error": "REAL_DATA_BOUNDARY", "message": str(e)}, args.json, f"拒绝：{e}")
        return 1
    except (sqlite3.IntegrityError, runner.ClockError) as e:
        _emit({"ok": False, "status": "ERROR", "error": "RUN", "message": str(e)}, args.json, f"错误：{e}")
        return 1
    tag = " [FIXTURE]" if r["run_mode"] == "fixture" else ""
    _emit({"ok": True, **r}, args.json,
          f"回放完成{tag}（{r['run_id']}）：{r['days']} 个交易日，{r['steps']:,} 步，机会 {r['setups']}，"
          f"订单 {r['orders']}，交易 {r['trades']}，合计 {r['pnl_ticks']} tick。\n这是纸面模拟，不是投资建议。")
    return 0


def _baseline(args, settings, con, scope, registry) -> int:
    from .engine import runner
    from .research import baseline as bl
    from .research import splits
    from .research.run import run_baseline
    log = (lambda m: None) if args.json else (lambda m: print(m, file=sys.stderr, flush=True))
    try:
        if args.jobs < 1:
            raise ConfigError("--jobs 应为正整数")
        r = run_baseline(settings, con, scope, _registry(registry), allow_real_data=args.allow_real_data,
                         skip_power=args.skip_power, jobs=args.jobs if registry is None else 1, log=log)
    except ConfigError as e:
        _emit({"ok": False, "status": "ERROR", "error": "CONFIG", "message": str(e)}, args.json, f"错误：{e}")
        return 2
    except (runner.ReplayRefused, splits.SplitError, bl.BaselineError) as e:
        _emit({"ok": False, "status": "REFUSED", "error": type(e).__name__, "message": str(e)}, args.json, f"拒绝：{e}")
        return 1
    t = r["total"]
    num = lambda x, nd=3: "—" if x is None else f"{float(x):.{nd}f}"       # noqa: E731
    lines = [f"基线 {r['baseline_id']}：{r['status']}" + ("（复用已冻结的结果）" if r["reused"] else "")
             + ("  [FIXTURE_ONLY]" if "FIXTURE_ONLY" in r["flags"] else ""),
             f"合计：{t['days']} 个有效交易日，{t['trades']} 笔交易，净盈亏 ${float(t['net_usd']):,.2f}，"
             f"净 R／日 {num(t['net_r_per_day'], 4)}，胜率 {num(t['win_rate'], 3)}，"
             f"PF {'不可估' if t['profit_factor'] is None else num(t['profit_factor'], 2)}。",
             f"切分：胶囊 {r['splits']['capsules']} 个，按默认流程可评测 {r['splits']['candidates_supported']} 个候选。"]
    if r["power"]:
        lines.append("短窗正式资格：" + "；".join(f"{k} {v['eligibility']}" for k, v in r["power"].items()))
    if r.get("power_note"):
        lines.append(r["power_note"])
    lines += [f"报告：{v}" for k, v in r["reports"].items() if k in ("baseline", "power")]
    lines.append("这是纸面模拟，不是投资建议。")
    _emit(r, args.json, "\n".join(lines))
    return 0 if r["ok"] else 1


def _account(args, manual_cfg) -> str | None:
    return args.account or manual_cfg.default_account_id


def _snapshot_time(con, account_id: str) -> int | None:
    r = con.execute("SELECT MAX(fetched_at_utc) FROM broker_snapshots WHERE account_id=?", (account_id,)).fetchone()
    return r[0]


def _plan(args, settings, con, scope, registry) -> int:
    import time

    from .engine.config import load_risk
    from .manual import plans
    from .manual.config import load_manual
    from .strategies.registry import default_registry

    manual_cfg, risk_cfg = load_manual(settings.project_root), load_risk(settings.project_root)
    strategy = (registry or default_registry()).get(scope.strategy_family)
    now = int(time.time())
    tick = settings.instrument(scope.instrument_root).tick_size
    px = lambda t: "无" if t is None else str(t * tick)       # noqa: E731
    try:
        if args.list:
            plans.expire_due(con, now)
            rows = plans.list_plans(con, args.account)
            text = "\n".join(f"{r['plan_id']}  {r['status']:<16} {r['contract_id']} {r['side']} {r['quantity']} 手  "
                             f"入场 {px(r['entry_ticks'])} 止损 {px(r['stop_ticks'])} 目标 {px(r['target_ticks'])}  "
                             f"{r['conclusion'] or ''}" for r in rows) or "没有计划。"
            _emit({"plans": rows}, args.json, text)
            return 0
        if args.cancel:
            r = plans.cancel(con, args.cancel, now)
            _emit(r, args.json, f"{r['plan_id']} 已取消。")
            return 0
        if args.activate:
            p = plans.get(con, args.activate)
            r = plans.activate(settings, con, args.activate, scope=scope, strategy=strategy, risk_cfg=risk_cfg,
                               manual_cfg=manual_cfg, now_utc=now,
                               snapshot_fetched_at=_snapshot_time(con, p["account_id"]))
            text = (f"{r['plan_id']} 已激活，已预留风险预算。" if r["status"] == "ACTIVE" else
                    f"{r['plan_id']} 没有激活，保持 DRAFT：{r['check']['risk_reason'] or r['check']['conclusion']}。")
            _emit(r, args.json, text + "\n" + r["check"]["summary"])
            return 0 if r["status"] == "ACTIVE" else 1
        account = _account(args, manual_cfg)
        if not account:
            raise plans.PlanError("没有账户标识：用 --account 指定，或在 config/manual.yaml 里填 default_account_id")
        inp = plans.parse_quick(args.tokens, settings, sessions.trading_day_at(now))
        if inp.instrument_root != scope.instrument_root:
            scope = parse_scope(f"{scope.strategy_family}:{inp.instrument_root}:{scope.session_type}:{scope.timeframe}",
                                settings)
        r = plans.create(settings, con, account_id=account, scope=scope, strategy=strategy, inp=inp, risk_cfg=risk_cfg,
                         manual_cfg=manual_cfg, now_utc=now, snapshot_fetched_at=_snapshot_time(con, account))
        text = (f"计划 {r['plan_id']}（DRAFT，未预留预算）：{inp.instrument_root} 解析为实际合约 {r['contract_id']}。\n"
                f"{r['check']['summary']}\n激活：python -m trading_agent plan --activate {r['plan_id']}")
        _emit(r, args.json, text)
        return 0
    except (plans.PlanError, ConfigError) as e:
        _emit({"status": "ERROR", "error": "PLAN", "message": str(e)}, args.json, f"错误：{e}")
        return 2


def _watch(args, settings, con, scope, registry, adapter) -> int:
    from .engine.config import load_risk
    from .manual import watch as watch_mod
    from .manual.broker import FileSnapshotAdapter
    from .manual.config import load_manual
    from .strategies.registry import default_registry

    manual_cfg, risk_cfg = load_manual(settings.project_root), load_risk(settings.project_root)
    account = _account(args, manual_cfg)
    if not account:
        _emit({"status": "ERROR", "error": "CONFIG", "message": "没有账户标识"}, args.json,
              "错误：没有账户标识：用 --account 指定，或在 config/manual.yaml 里填 default_account_id")
        return 2
    try:
        interval = watch_mod.resolve_interval(settings.watch, args.interval)
    except ValueError as e:
        _emit({"status": "ERROR", "error": "CONFIG", "message": str(e)}, args.json, f"错误：{e}")
        return 2
    strategy = (registry or default_registry()).get(scope.strategy_family)
    adapter = adapter or FileSnapshotAdapter(settings)

    def poll():
        return watch_mod.poll_once(settings, con, adapter, account, scope=scope, strategy=strategy, risk_cfg=risk_cfg,
                                   manual_cfg=manual_cfg, interval_seconds=interval)

    def emit(r):
        lines = [f"[watch] {r['as_of_utc']} 账户 {account} 间隔 {interval}s 状态 {r['status']}"
                 + (f"（{r['message']}）" if r.get("message") else "")]
        lines += [f"  [{a['level']}] {a['message']} 数据时点 {a['data_as_of_utc']}。" for a in r["alerts"]]
        _emit(r, args.json, "\n".join(lines))

    try:
        watch_mod.run_loop(poll, interval, iterations=1 if args.once else None, emit=emit)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
