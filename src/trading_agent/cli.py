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
    "memory": 7, "evaluate": 8, "shadow": 9, "release": 9, "rollback": 9, "freeze": 9, "monitor": 9,
}
IMPLEMENTED = {"ingest", "collect", "scan", "plan", "watch", "replay", "baseline", "review", "evolve", "memory"}
HELP = {
    "ingest": "扫描数据根目录，增量导入行情并生成数据可得性报告",
    "collect": "只读采集已收线的 K 线并入库。目前唯一的适配器是 Webull，而该账号没有期货行情，所以暂不可用",
    "scan": "数据校验后运行策略和纸面账本，输出 JSON 和中文短报",
    "plan": "人工交易前登记与预检（只给结论，不下单）", "watch": "盘中只读纪律提醒",
    "replay": "在一段日期上回放策略和纸面账本（与 scan 共用同一个引擎）",
    "baseline": "冻结基线：登记数据切分、回放两次校验一致、生成基线报告和功效测算",
    "review": "为已封存的 session 生成日复盘（规则生成，不调用模型）", "evolve": "跨日聚合已复盘的观察，生成候选并维护候选台账和队列（不调用模型，不读保留集）", "evaluate": "独立评测",
    "memory": "长期记忆：列出、确认、拒绝、否定、回滚（条目只有结构化字段，不调用模型）",
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
            sp.add_argument("--alpaca", action="store_true",
                            help="从 Alpaca 模拟盘读取订单记录和成交并导入（只读；账户取 --account 或默认账户）")
            sp.add_argument("--account", help="--alpaca 时的账户标识")
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
        if name == "evolve":
            sp.add_argument("--last", type=int, metavar="N",
                            help="读最近 N 个已封存并已复盘的 session（缺省取 config/evolve.yaml，0 = 全部）")
            sp.add_argument("--run-id", help="策略轨的运行标识（缺省取该 scope 已冻结基线的主运行）")
            sp.add_argument("--account", help="带上这个账户的人工轨观察")
            sp.add_argument("--import-proposals", metavar="JSON", help="导入提案文件（逐条校验，内容只当数据）")
            sp.add_argument("--estimate", action="store_true",
                            help="为队列里还没有效应估计的候选回放开发段和向前验证段，然后重排（每个约几分钟）")
            sp.add_argument("--pre-eval-shadow", action="store_true", help="对排名前几的候选做前向预演（只查可运行性）")
            sp.add_argument("--ledger", action="store_true", help="只显示队列、台账统计和余下的 α 额度")
        if name == "memory":
            sp.add_argument("--confirm", metavar="候选标识", help="确认一条 Memory 候选（偏好立即生效；其余等评测）")
            sp.add_argument("--reject", metavar="候选标识", help="拒绝一条 Memory 候选")
            sp.add_argument("--reason", default="NOT_WANTED", help="拒绝原因：NOT_WANTED、WRONG、DUPLICATE、INVALID")
            sp.add_argument("--supersede", metavar="ITEM_ID", help="否定一条生效的条目（标 SUPERSEDED，保留历史）")
            sp.add_argument("--evidence", action="append", default=[], metavar="事件标识", help="否定所依据的证据，可重复")
            sp.add_argument("--rollback", metavar="MEMORY_VERSION", help="把头指针指回一个已有的版本")
            sp.add_argument("--history", action="store_true", help="显示最近的 Memory 事件")
            sp.add_argument("--context", metavar="任务类型", help="显示某类任务会注入的条目（如 SessionReview、watch）")
        if name in ("replay", "baseline", "evolve"):
            sp.add_argument("--allow-real-data", action="store_true",
                            help="允许在真实数据上运行（还需要已设置开发可见截止日，且区间不晚于它）")
        if name == "review":
            sp.add_argument("--day", metavar="YYYY-MM-DD", help="复盘这一个交易日")
            sp.add_argument("--from", dest="start", metavar="YYYY-MM-DD", help="区间起始交易日")
            sp.add_argument("--to", dest="end", metavar="YYYY-MM-DD", help="区间结束交易日")
            sp.add_argument("--run-id", help="运行标识（缺省取该 scope 已冻结基线的主运行）")
            sp.add_argument("--account", help="带上这个账户的人工轨")
            sp.add_argument("--seal", action="store_true", help="复盘前先封存 session")
            sp.add_argument("--manual-only", action="store_true",
                            help="只复盘人工轨（账户取 --account 或默认账户）：不需要策略运行，也不读行情")
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
        if args.command == "ingest" and args.alpaca:
            from .manual import alpaca as alpaca_mod
            from .manual.broker import BrokerReadError
            from .manual.config import load_manual, risk_for_account
            manual_cfg = load_manual(settings.project_root)
            account = args.account or manual_cfg.default_account_id
            if not account:
                _emit({"ok": False, "status": "ERROR", "error": "CONFIG", "message": "没有账户标识"}, args.json,
                      "错误：没有账户标识：用 --account 指定，或在 config/manual.yaml 里填 default_account_id")
                return 2
            try:
                out = alpaca_mod.sync(settings, con, adapter or alpaca_mod.adapter_for(settings, account), account,
                                      risk_cfg=risk_for_account(settings.project_root, account), manual_cfg=manual_cfg)
            except BrokerReadError as e:
                _emit({"ok": False, "status": "ERROR", "error": "BROKER_READ", "message": str(e)}, args.json, f"读取失败：{e}")
                return 1
            ev, tr = out["results"]
            _emit({"ok": True, **out}, args.json,
                  f"Alpaca {account}：订单事件导入 {ev['imported']}、跳过 {ev['skipped']}；成交导入 {tr['imported']}、"
                  f"跳过 {tr['skipped']}、隔离 {tr.get('quarantined', 0)}、无法解析 {tr['bad_rows']}"
                  + (f"；忽略未登记的代码 {sorted(out['ignored_symbols'])}" if out["ignored_symbols"] else "") + "。")
            return 0

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

        if args.command == "review":
            return _review(args, settings, con, scope)

        if args.command == "memory":
            return _memory(args, settings, con, scope)
        if args.command == "evolve":
            return _evolve(args, settings, con, scope, registry)

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


def _review(args, settings, con, scope) -> int:
    from collections import Counter

    from .review import build as build_mod
    from .review.seal import SealError
    try:
        if args.day and (args.start or args.end):
            raise ValueError("--day 和 --from／--to 不能同时给")
        if args.day:
            start = end = date.fromisoformat(args.day)
        elif args.start and args.end:
            start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        else:
            raise ValueError("需要 --day，或 --from 加 --to")
        if end < start:
            raise ValueError("--to 不能早于 --from")
        run_id = args.run_id
        if args.manual_only:
            from .manual.config import load_manual
            run_id, scope = None, None
            args.account = args.account or load_manual(settings.project_root).default_account_id
            if not args.account:
                raise ValueError("只复盘人工轨时需要账户：用 --account 指定，或在 config/manual.yaml 里填 default_account_id")
        elif not run_id:
            r = con.execute("SELECT run_id FROM baselines WHERE scope=? AND status='FROZEN' AND run_id IS NOT NULL"
                            " ORDER BY updated_at_utc DESC LIMIT 1", (str(scope),)).fetchone()
            if r is None:
                raise ValueError(f"没有给 --run-id，{scope} 也没有已冻结的基线")
            run_id = r[0]
    except ValueError as e:
        _emit({"ok": False, "status": "ERROR", "error": "CONFIG", "message": str(e)}, args.json, f"错误：{e}")
        return 2
    results, skipped = [], Counter()
    for d in sessions.trading_days(start, end):
        try:
            results.append(build_mod.run(settings, con, scope, run_id, d.isoformat(), account_id=args.account,
                                         seal_first=args.seal))
        except (SealError, build_mod.ReviewError) as e:
            skipped[e.code] += 1
            if start == end:
                _emit({"ok": False, "status": "REFUSED", "error": e.code, "message": str(e)}, args.json, f"拒绝：{e}")
                return 1
    attr, obs = Counter(), Counter()
    for r in results:
        attr.update(r["attribution"])
        obs.update(r["observations"])
    out = {"ok": bool(results), "run_id": run_id, "scope": str(scope) if scope else f"manual:{args.account}",
           "reviewed": len(results),
           "skipped": dict(skipped), "trades": sum(r["trades"] for r in results),
           "incidents": sum(r["incidents"] for r in results), "candidates": sum(r["candidates"] for r in results),
           "attribution": dict(attr), "observations": dict(obs),
           "results": results if len(results) <= 5 else results[:2] + results[-2:]}
    if len(results) == 1:
        r = results[0]
        text = (f"复盘 {r['session_id']}：交易 {r['trades']} 笔，净 {r['net_usd']} 美元；机会允许 {r['allowed']}、拦截 {r['blocked']}；"
                f"观察 {len(r['observations'])} 条，事故草稿 {r['incidents']} 份。\n报告：{r['reports']['markdown']}")
    else:
        text = (f"复盘 {len(results)} 个 session（{'运行 ' + run_id if run_id else '人工轨，账户 ' + args.account}）"
                + (f"，跳过 {dict(skipped)}" if skipped else "") + f"：交易 {out['trades']} 笔，事故草稿 {out['incidents']} 份。\n"
                + "归因：" + "、".join(f"{k} {v}" for k, v in sorted(attr.items())) + "\n"
                + "观察（按天数）：" + "、".join(f"{k} {v}" for k, v in obs.most_common()) + "\n"
                + "报告目录：" + str(settings.project_root / "reports" / "sessions"))
    _emit(out, args.json, text + "\n这是纸面模拟和执行记录的复盘，不是投资建议。")
    return 0 if results else 1


def _memory(args, settings, con, scope) -> int:
    from .memory import context as mctx
    from .memory import items as mitems
    from .memory import store
    from .memory.registry import load_registry

    acts = [a for a in ("confirm", "reject", "supersede", "rollback") if getattr(args, a)]
    try:
        reg = load_registry(settings.project_root)
        if len(acts) > 1:
            raise ValueError("--confirm、--reject、--supersede、--rollback 一次只能给一个")
        if args.confirm:
            r = store.confirm(settings, con, args.confirm, reg=reg)
            text = (f"已确认，生效：{r['sentence']}\nmemory_version：{r['memory_version']}" if r["item_id"] else
                    f"已确认，等待评测（阶段 8）后才生效：{r['sentence']}")
        elif args.reject:
            r = store.reject(con, args.reject, reason=args.reason)
            text = f"已拒绝候选 {args.reject}。"
        elif args.supersede:
            r = store.supersede(settings, con, args.supersede, evidence=args.evidence)
            text = f"条目 {r['item_id']} 已标为 SUPERSEDED。memory_version：{r['memory_version']}"
        elif args.rollback:
            r = store.rollback(con, args.rollback)
            text = f"已回滚到 {r['memory_version']}（{r['items']} 条），之前是 {r['previous_memory_version']}。"
        elif args.history:
            r = {"events": store.history(con)}
            text = "\n".join(f"{e['at']} {e['event']} {e['item_id'] or e['memory_candidate_id'] or ''}"
                             f" {e['from'] or '—'} → {e['to'] or '—'}（{e['actor']}）" for e in r["events"]) or "还没有 Memory 事件。"
        elif args.context:
            r = mctx.build(settings, con, scope, args.context, reg=reg)
            text = (f"{args.context} 会注入 {len(r['items'])} 条（memory_version {r['memory_version'] or '无'}）：\n"
                    + "\n".join("  " + i["sentence"] for i in r["items"]))
        else:
            store.sweep(con)
            loaded = mctx.load(settings, con, scope, reg=reg)
            pend = []
            for c in store.candidates(con):
                try:
                    c["sentence"], c["invalid"] = mitems.sentence(settings, reg, mitems.validate(settings, reg, c["body"])), None
                except mitems.Invalid as e:
                    c["sentence"], c["invalid"] = None, e.reason
                pend.append(c)
            r = {**loaded, "candidates": pend}
            text = (f"memory_version：{loaded['memory_version'] or '无'}，scope {loaded['scope']} 下生效 {len(loaded['items'])} 条"
                    + (f"，排除 {loaded['excluded']}" if loaded["excluded"] else "") + "\n"
                    + "\n".join("  " + i["sentence"] for i in loaded["items"])
                    + (f"\n待处理的候选 {len(pend)} 条：\n" if pend else "\n没有待处理的候选。")
                    + "\n".join(f"  {c['memory_candidate_id']}（{c['status']}）"
                                + (c["sentence"] if c["sentence"] else f"不合法：{c['invalid']}") for c in pend))
    except (store.MemoryError_, db.StaleVersion) as e:
        code = getattr(e, "reason", "STALE_VERSION")
        _emit({"ok": False, "status": "REFUSED", "error": code, "message": str(e)}, args.json, f"拒绝（{code}）：{e}")
        return 1
    except (ValueError, KeyError, ConfigError) as e:
        _emit({"ok": False, "status": "ERROR", "error": "CONFIG", "message": str(e)}, args.json, f"错误：{e}")
        return 2
    _emit({"ok": True, **r}, args.json, text.rstrip())
    return 0


def _evolve(args, settings, con, scope, registry) -> int:
    from .engine import runner
    from .engine.config import load_engine, load_risk
    from .evolve import candidates as cands
    from .evolve import queue
    from .evolve import run as ev
    from .evolve.aggregate import EvolveError
    from .evolve.config import load_evolve
    from .research import splits
    from .research.config import load_research
    log = (lambda m: None) if args.json else (lambda m: print(m, file=sys.stderr, flush=True))
    reg = _registry(registry)
    tail = "\n候选只是待评测的假设，不经独立评测和人工批准不会生效。这不是投资建议。"
    try:
        if sum(map(bool, (args.estimate, args.pre_eval_shadow, args.ledger))) > 1:
            raise ConfigError("--estimate、--pre-eval-shadow、--ledger 一次只能用一个")
        cfg, rcfg = load_evolve(settings.project_root), load_research(settings.project_root)
        if args.estimate:
            r = ev.estimate_queue(settings, con, scope, reg, cfg=cfg, research_cfg=rcfg,
                                  allow_real_data=args.allow_real_data, log=log)
            text = (f"估计了 {len(r['estimated'])} 个候选。\n" + _queue_text(r["queue"]))
        elif args.pre_eval_shadow or args.ledger:
            base = ev.frozen_baseline(con, str(scope))
            queue.supersede(con, str(scope), queue.base_release_of(base["baseline_id"]), cfg)
            ranked = queue.rank(con, str(scope), cfg, rcfg.power)
            if args.ledger:
                r = {"ok": True, "scope": str(scope), "baseline_id": base["baseline_id"], "queue": ranked,
                     "alpha": cands.alpha_summary(con, str(scope), rcfg.power),
                     "candidates": dict(con.execute("SELECT status, COUNT(*) FROM candidates WHERE scope=? GROUP BY 1",
                                                    (str(scope),)).fetchall())}
                text = ("候选：" + ("、".join(f"{k} {v}" for k, v in sorted(r["candidates"].items())) or "无") + "\n"
                        + _queue_text(ranked) + "\n" + "\n".join(
                            f"{fam}：开发尝试 {a['dev_attempts']}，正式检验已登记 {a['formal_registered']}、"
                            f"还可登记 {a['formal_tests_left']}，余下 α {a['alpha_remaining']:g}" for fam, a in r["alpha"].items()))
            else:
                r = {"ok": True, "scope": str(scope), **queue.pre_eval_shadow(
                    settings, con, scope, reg, base, ranked, cfg=cfg, risk_cfg=load_risk(settings.project_root),
                    engine_cfg=load_engine(settings.project_root), allow_real_data=args.allow_real_data)}
                text = (f"前向预演：{r['status']}。{r['note']}" if r["status"] != "DONE" else
                        f"前向预演：{len(r['days'])} 个交易日，" + "；".join(
                            f"{x['candidate_id']} {'通过' if x['ok'] else '已看过' if x['ok'] is None else '未通过'}"
                            for x in r["results"]) + f"。{r['note']}")
        else:
            props = ev.load_proposals(args.import_proposals) if args.import_proposals else None
            r = ev.run(settings, con, scope, reg, run_id=args.run_id, account_id=args.account, last_n=args.last,
                       cfg=cfg, research_cfg=rcfg, proposals=props)
            w = r["windows"][0]
            text = (f"聚合 {r['scope']}：{w['sessions']} 个 session（{w['first_day']} 至 {w['last_day']}），"
                    f"聚类 {len(r['clusters'])} 个，可升级 {r['promotable']} 个。\n"
                    f"结果 {r['result']}：新候选 {r['created']} 个，拒绝 {len(r['rejected'])} 个，"
                    f"转 SUPERSEDED {len(r['superseded'])} 个。\n"
                    + "".join(f"  {c['candidate_id']} {c['eval_track']} {c['key']}：{c['from']} → {c['to']}"
                              f"（{c['evidence_days']} 天，{c['action']}）\n" for c in r["candidates"])
                    + "".join(f"  拒绝 {x['source']}：{x['reason']}\n" for x in r["rejected"])
                    + _queue_text(r["queue"]) + f"\n周报：{r['reports']['markdown']}")
    except ConfigError as e:
        _emit({"ok": False, "status": "ERROR", "error": "CONFIG", "message": str(e)}, args.json, f"错误：{e}")
        return 2
    except EvolveError as e:
        _emit({"ok": False, "status": "REFUSED", "error": e.code, "message": str(e)}, args.json, f"拒绝：{e}")
        return 1
    except (runner.ReplayRefused, splits.SplitError) as e:
        _emit({"ok": False, "status": "REFUSED", "error": type(e).__name__, "message": str(e)}, args.json, f"拒绝：{e}")
        return 1
    _emit(r, args.json, text + tail)
    return 0


def _queue_text(ranked: list[dict]) -> str:
    if not ranked:
        return "队列为空。"
    return "队列：" + "；".join(
        f"{q['rank']}. {q['candidate_id']}（{q['eval_track']}，"
        + ("未估计" if q["score"] is None else f"分数 {q['score']:.4f}，日差 {q['effect']['mean_diff_per_day']:+.4f} R")
        + "）" for q in ranked)


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

    from .manual import common
    from .manual.config import risk_for_account
    manual_cfg, risk_cfg = load_manual(settings.project_root), load_risk(settings.project_root)
    strategy = (registry or default_registry()).get(scope.strategy_family)
    now = int(time.time())

    def px(t, cid):                                   # 每份计划按它自己品种的 tick 显示价格
        return "无" if t is None else str(t * common.instrument_of(settings, cid).tick_size)
    try:
        if args.list:
            plans.expire_due(con, now)
            rows = plans.list_plans(con, args.account)
            text = "\n".join(f"{r['plan_id']}  {r['status']:<16} {r['contract_id']} {r['side']} {r['quantity']}  "
                             f"入场 {px(r['entry_ticks'], r['contract_id'])} 止损 {px(r['stop_ticks'], r['contract_id'])} "
                             f"目标 {px(r['target_ticks'], r['contract_id'])}  "
                             f"{r['conclusion'] or ''}" for r in rows) or "没有计划。"
            _emit({"plans": rows}, args.json, text)
            return 0
        if args.cancel:
            r = plans.cancel(con, args.cancel, now)
            _emit(r, args.json, f"{r['plan_id']} 已取消。")
            return 0
        if args.activate:
            p = plans.get(con, args.activate)
            _refresh_snapshot(settings, con, p["account_id"], scope, strategy, manual_cfg)
            r = plans.activate(settings, con, args.activate, scope=scope, strategy=strategy,
                               risk_cfg=risk_for_account(settings.project_root, p["account_id"], base=risk_cfg),
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
        _refresh_snapshot(settings, con, account, scope, strategy, manual_cfg)
        r = plans.create(settings, con, account_id=account, scope=scope, strategy=strategy, inp=inp,
                         risk_cfg=risk_for_account(settings.project_root, account, base=risk_cfg),
                         manual_cfg=manual_cfg, now_utc=now, snapshot_fetched_at=_snapshot_time(con, account))
        text = (f"计划 {r['plan_id']}（DRAFT，未预留预算）：{inp.instrument_root} 解析为实际合约 {r['contract_id']}。\n"
                f"{r['check']['summary']}\n激活：python -m trading_agent plan --activate {r['plan_id']}")
        _emit(r, args.json, text)
        return 0
    except (plans.PlanError, ConfigError) as e:
        _emit({"status": "ERROR", "error": "PLAN", "message": str(e)}, args.json, f"错误：{e}")
        return 2


def _broker_adapter(settings, account):
    """按 config/accounts.yaml 里登记的券商选只读适配器。没登记的账户读快照文件。"""
    from .manual.broker import FileSnapshotAdapter
    from .manual.config import load_accounts
    if (load_accounts(settings.project_root).get(account) or {}).get("broker") == "alpaca":
        from .manual import alpaca as alpaca_mod
        return alpaca_mod.adapter_for(settings, account)
    return FileSnapshotAdapter(settings)


def _refresh_snapshot(settings, con, account, scope, strategy, manual_cfg) -> None:
    """登记或激活计划前，先读一次账户（只对登记了 Alpaca 的账户）。读不到不报错：预检会如实说账户状态未知。"""
    from .manual import watch as watch_mod
    from .manual.config import load_accounts, risk_for_account
    if (load_accounts(settings.project_root).get(account) or {}).get("broker") != "alpaca":
        return
    try:
        watch_mod.poll_once(settings, con, _broker_adapter(settings, account), account, scope=scope, strategy=strategy,
                            risk_cfg=risk_for_account(settings.project_root, account), manual_cfg=manual_cfg,
                            interval_seconds=settings.watch["poll_interval_seconds"])
    except Exception:  # noqa: BLE001
        pass


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
        override = args.interval
        if override is None:                # 已生效的偏好排在命令行之后、配置文件之前
            from .memory import context as mctx
            override = mctx.preferences(settings, con, scope).get("watch_poll_interval_seconds")
        interval = watch_mod.resolve_interval(settings.watch, override)
    except ValueError as e:
        _emit({"status": "ERROR", "error": "CONFIG", "message": str(e)}, args.json, f"错误：{e}")
        return 2
    strategy = (registry or default_registry()).get(scope.strategy_family)
    adapter = adapter or _broker_adapter(settings, account)
    from .manual.config import risk_for_account
    risk_cfg = risk_for_account(settings.project_root, account, base=risk_cfg)

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
