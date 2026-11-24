"""盘中主来源：读 ~/trading-desk 的 price-feed 流水线存在本地的 TradingView 5 分钟线。

2026-11-24 用户决定：本地数据为主、Webull 为备用（此前是 Webull 为主）。

只读本地 JSON，不连 TradingView、Webull 或任何行情 API，也不往那个目录写东西。

- 品种：ES1!、NQ1!（CME_MINI 标准合约的 TradingView 连续合约；2026-11-23 起 price-feed 从 MES1!／MNQ1! 改为它们）。按用户决定当作 ES／NQ 的行情：
  合约标识按 roll-v2 记实际合约，来源记为 tvfeed，来源质量不降级。
- 换月：TradingView 的连续合约在到期日前后才切月，而系统按 roll-v2 在到期周的星期一就切到下一季。
  到期周里两边可能不是同一个合约，这几天不写本地线，当天由 Webull 接管。
- 只放行晚于开发截止日、且在实时行情放行首日及之后的交易日（config/live_data.yaml）；之前的日子一律不读。
- 只用收线时刻不晚于这份文件取数时刻的 K 线（没走完的最后一根不要）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import contracts, live_window, sessions
from .collect import _append, _existing, _norm
from .config import Settings

SOURCE = "tvfeed"
TF, SIZE = "5m", 300


def config(project_root) -> dict:
    import yaml
    p = Path(project_root) / "config" / "live_data.yaml"
    raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.is_file() else {}
    return raw.get("tvfeed") or raw.get("backup") or {}


def csv_path(settings: Settings, root: str, cid: str, day: str) -> Path:
    return settings.data_root / SOURCE / root / cid / f"{day}.csv"


def front_contract(inst, day: date) -> str:
    """到期日不早于 day 的最近一个季月合约：TradingView 连续合约（1!）这一天指的合约。"""
    chain = contracts.contract_chain(inst, day.year - 1, day.year + 1)
    return min((c for c in chain if contracts.expiry(c) >= day), key=contracts.expiry)


def _latest_files(day_dir: Path, symbol: str) -> Path | None:
    files = sorted(day_dir.glob(f"{symbol}-*.bars.json"))
    return files[-1] if files else None


def _fetched(obj: dict) -> int | None:
    try:
        return int(datetime.fromisoformat(obj["fetched_at"]).timestamp())
    except (KeyError, ValueError, TypeError):
        return None


def collect(settings: Settings, con: sqlite3.Connection, *, now: int | None = None, ingest: bool = True,
            feed_root: Path | None = None, cfg: dict | None = None) -> dict:
    """把 price-feed 里最近几个日期目录的最新文件合并，写成数据合同 CSV（来源 tvfeed），再导入。"""
    from .ingest import new_run_id
    from .pipeline import run_ingest
    now = int(time.time()) if now is None else now
    cfg = cfg if cfg is not None else config(settings.project_root)
    root_dir = Path(feed_root or cfg.get("feed_root") or "")
    symbols = cfg.get("symbols") or {}
    if not root_dir.is_dir() or not symbols:
        return {"ok": False, "status": "NOT_CONFIGURED", "feed_root": str(root_dir)}
    lw = live_window.effective(con, settings.project_root)
    cut = settings.dev_visible_through.isoformat() if settings.dev_visible_through else None
    stats = {"files_read": 0, "bars_seen": 0, "unclosed": 0, "not_live": 0, "roll_window": 0, "outside": 0,
             "existing": 0, "new_bars": 0, "revisions": 0}
    merged: dict[tuple[str, int], tuple[dict, int]] = {}
    for day_dir in sorted(p for p in root_dir.iterdir() if p.is_dir()):
        try:
            dir_day = date.fromisoformat(day_dir.name).isoformat()
        except ValueError:
            continue
        for symbol, root in symbols.items():
            start = lw.starts(root)
            if start is None or dir_day < start:
                continue                                   # 放行首日之前的日子不读（封存期）
            f = _latest_files(day_dir, symbol)
            if f is None:
                continue
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            fetched = _fetched(obj)
            if fetched is None:
                continue
            stats["files_read"] += 1
            for b in obj.get("m5") or []:
                try:
                    s = int(b["time"])
                except (KeyError, ValueError, TypeError):
                    continue
                stats["bars_seen"] += 1
                if s + SIZE > min(fetched, now):
                    stats["unclosed"] += 1                 # 取数时还没走完
                    continue
                key = (root, s)
                if key not in merged or merged[key][1] <= fetched:
                    merged[key] = (b, fetched)              # 新文件覆盖旧的
    include_eth = settings.collect.get("include_eth", True)
    per_file: dict[Path, list[list[str]]] = {}
    cache: dict[Path, dict] = {}
    for (root, s), (b, fetched) in sorted(merged.items()):
        d, sess = sessions.classify(s)
        if d is None or (sess == "ETH" and not include_eth):
            stats["outside"] += 1
            continue
        ds = d.isoformat()
        if not lw.allows(SOURCE, root, ds) or (cut and ds <= cut):
            stats["not_live"] += 1
            continue
        inst = settings.instrument(root)
        cid = contracts.active_contract(inst, d, settings.roll_rules)
        if front_contract(inst, d) != cid:
            stats["roll_window"] += 1                      # 到期周：连续合约和系统的实际合约可能不同
            continue
        path = csv_path(settings, root, cid, ds)
        if path not in cache:
            cache[path] = _existing(path)
        content = tuple(_norm(Decimal(str(b[k]))) for k in ("open", "high", "low", "close")) + (_norm(b.get("volume", 0)),)
        k = (TF, sessions.iso(s))
        prev = cache[path].get(k)
        if prev == content:
            stats["existing"] += 1
            continue
        stats["revisions" if prev is not None else "new_bars"] += 1
        cache[path][k] = content
        per_file.setdefault(path, []).append([root, cid, TF, sessions.iso(s), *content, SOURCE,
                                              sessions.iso(s + SIZE), sessions.iso(fetched)])
    for path, rows in sorted(per_file.items()):
        _append(path, rows)
    written = sum(len(v) for v in per_file.values())
    out = {"ok": True, "source": SOURCE, **stats, "rows_written": written,
           "files": [str(p.relative_to(settings.data_root)) for p in sorted(per_file)]}
    if ingest and written:
        out["ingest"] = run_ingest(settings, con, run_id=new_run_id("tvf"), kind="collect", write_report=False)
    return out


def needs_fallback(settings: Settings, con: sqlite3.Connection, root: str, now: int | None = None) -> tuple[bool, str]:
    """此刻要不要取 Webull：本地线当天没有任何 RTH 线（price-feed 没跑，或到期周不用本地线）才需要。

    引擎每天只按优先级选一个来源：当天本地线一旦有了 RTH 线，就一直用它，这时取 Webull 也用不上。
    """
    from .scan import _session_for
    now = int(time.time()) if now is None else now
    day, in_rth = _session_for(now)
    inst = settings.instrument(root)
    cid = contracts.active_contract(inst, day, settings.roll_rules)
    if front_contract(inst, day) != cid:
        return True, "到期周：不用本地线"
    n = con.execute("SELECT COUNT(*) FROM bars WHERE source=? AND contract_id=? AND trading_day=? AND session_type='RTH'",
                    (SOURCE, cid, day.isoformat())).fetchone()[0]
    if n == 0 and in_rth:
        return True, "本地线今天还没有 RTH 线"
    return False, f"本地线今天已有 {n} 根 RTH 线"

