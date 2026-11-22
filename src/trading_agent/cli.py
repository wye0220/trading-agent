"""python -m trading_agent 命令行入口。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from . import db, sessions
from .config import DEFAULT_SCOPE, ConfigError, load_settings, parse_scope

# 子命令 -> 负责实现它的阶段。阶段 1 实现 ingest、collect 和最小 scan。
COMMANDS = {
    "ingest": 1, "collect": 1, "scan": 1,
    "plan": 4, "watch": 4, "replay": 5, "review": "6A", "evolve": "6B",
    "evaluate": 8, "shadow": 9, "release": 9, "rollback": 9, "freeze": 9, "monitor": 9,
}
IMPLEMENTED = {"ingest", "collect", "scan"}
HELP = {
    "ingest": "扫描数据根目录，增量导入行情并生成数据可得性报告",
    "collect": "从 Webull 只读采集已收线的 K 线，追加到 CSV 并入库",
    "scan": "阶段 1：只做数据校验和快照封存（WAIT 或 DATA_ERROR）",
    "plan": "人工交易前登记与预检", "watch": "盘中只读纪律提醒", "replay": "回放与基线",
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
        if name == "scan":
            sp.add_argument("--as-of", metavar="ISO8601", help="决策时点（UTC，或带时区）。缺省为当前时刻")
        if name not in IMPLEMENTED:
            sp.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return p


def _emit(obj: dict, as_json: bool, text: str | None = None):
    if as_json or text is None:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(text)


def main(argv: list[str] | None = None, *, settings=None, adapter=None) -> int:
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
        if args.command == "ingest":
            from .pipeline import run_ingest
            try:
                through = date.fromisoformat(args.through) if args.through else None
            except ValueError:
                _emit({"status": "ERROR", "error": "CONFIG", "message": f"--through 不是合法日期：{args.through}"},
                      args.json, f"错误：--through 不是合法日期：{args.through}")
                return 2
            db.open_release(settings).close()        # 只建表，不写 active 指针
            s = run_ingest(settings, con, through=through, write_report=not args.no_report)
            text = (f"导入完成（{s['run_id']}，{s['seconds']} 秒）：处理 {s['files_imported']} 个文件，跳过 {s['files_skipped']} 个，"
                    f"不支持 {s['files_unsupported']} 个，拒收 {s['files_rejected']} 个；新增 K 线 {s['bars_inserted']:,}，"
                    f"新 revision {s['revisions_added']:,}，跳过重复 {s['duplicates_skipped']:,}；"
                    f"丢弃价差代码 {s['spread_symbols_dropped']} 个、记录 {s['spread_records_dropped']:,} 条；"
                    f"截止日之后未入库 {s['withheld_after_cutoff']:,} 条。"
                    + (f"\n异常：{s['anomalies']}" if s["anomalies"] else "")
                    + ("\n标记：" + "、".join(s["flags"]) if s["flags"] else "")
                    + (f"\n报告：{s['report']}" if s.get("report") else ""))
            _emit(s, args.json, text)
            return 0

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
            _emit(s, args.json, text)
            return 0 if s["ok"] else 1

        if args.command == "scan":
            from .scan import run_scan
            try:
                as_of = sessions.parse_utc(args.as_of) if args.as_of else None
            except ValueError:
                _emit({"status": "ERROR", "error": "CONFIG", "message": f"--as-of 不是合法时刻：{args.as_of}"},
                      args.json, f"错误：--as-of 不是合法时刻：{args.as_of}")
                return 2
            r = run_scan(settings, con, scope, as_of)
            _emit(r, args.json, r["report"])
            return 0
    finally:
        con.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
