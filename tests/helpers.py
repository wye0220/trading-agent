"""测试辅助：生成合成（FIXTURE）行情。所有合成数据的 source 以 fixture 开头或文件名含 fixture。"""
from __future__ import annotations

import csv
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as N

from trading_agent import sessions
from trading_agent.config import load_settings

PROJECT = Path(__file__).resolve().parents[1]


def _neutral_project() -> Path:
    """测试用的项目根：config/ 是真实配置的副本，但把由用户填写的值恢复成"未配置"。

    这样测试结果不随 config/ 里的账户限额、费用、开发可见截止日等个人设置变化。其余目录链接到真实项目。
    """
    import atexit
    import shutil
    import tempfile

    import yaml

    root = Path(tempfile.mkdtemp(prefix="trading-agent-test-project-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    shutil.copytree(PROJECT / "config", root / "config")
    for name in ("schemas", "src", "docs", "tests"):
        (root / name).symlink_to(PROJECT / name, target_is_directory=True)

    def edit(rel, fn):
        f = root / "config" / rel
        raw = yaml.safe_load(f.read_text(encoding="utf-8"))
        fn(raw)
        f.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def risk(r):
        r["fees"]["amount_usd"] = None
        r["paper"]["research_risk_unit_usd"] = None
        r["account"].update(approved=False, budget_per_trade_usd=None, daily_loss_limit_usd=None, max_contracts=None)

    edit("paths.yaml", lambda r: r.update(dev_visible_through=None))
    edit("risk.yaml", risk)
    edit("manual.yaml", lambda r: r.update(default_account_id=None))

    def accounts(r):
        for a in (r.get("accounts") or {}).values():
            a["approved"] = False

    if (root / "config" / "accounts.yaml").is_file():
        edit("accounts.yaml", accounts)
    return root


TEST_PROJECT = _neutral_project()
COLUMNS = ["instrument_root", "contract_id", "timeframe", "bar_start_utc", "open", "high", "low", "close",
           "volume", "source", "fetched_at_utc", "fixture"]


def make_settings(tmp_path: Path, **kw):
    return load_settings(TEST_PROJECT, env={}, data_root=tmp_path / "data-root",
                         release_db=tmp_path / "release" / "release-state.sqlite", **kw)


def et(day: date, hh: int, mm: int = 0, ss: int = 0) -> int:
    return sessions.ts(datetime.combine(day, time(hh, mm, ss), sessions.NY))


def bar(start: int, price: float = 5000.0, *, contract="ESZ2025", tf="5m", source="fixsrc", volume=100,
        fetched: int | None = None, fixture=True, high=None, low=None, close=None, root="ES") -> dict:
    p = Decimal(str(price))
    return {"instrument_root": root, "contract_id": contract, "timeframe": tf,
            "bar_start_utc": sessions.iso(start), "open": str(p),
            "high": str(Decimal(str(high)) if high is not None else p + 1),
            "low": str(Decimal(str(low)) if low is not None else p - 1),
            "close": str(Decimal(str(close)) if close is not None else p + Decimal("0.5")),
            "volume": str(volume), "source": source,
            "fetched_at_utc": sessions.iso(fetched) if fetched else "", "fixture": "1" if fixture else ""}


def rth_bars(day: date, *, step=300, skip=(), base=5000.0, **kw) -> list[dict]:
    """一个 RTH 交易日的全部 K 线。skip 是要去掉的 (hh, mm) 列表。"""
    skip_ts = {et(day, h, m) for h, m in skip}
    out = []
    for i, s in enumerate(sessions.expected_rth_slots(day, step)):
        if s not in skip_ts:
            out.append(bar(s, base + (i % 7) * 0.25, tf="5m" if step == 300 else "1m", **kw))
    return out


def eth_bars(day: date, *, step=300, base=4990.0, hours=(3, 9), **kw) -> list[dict]:
    """当日 RTH 开盘前的一段夜盘（默认 03:00–09:30 ET）。"""
    out = []
    s = et(day, hours[0])
    end = sessions.rth_bounds(day)[0]
    i = 0
    while s < end:
        out.append(bar(s, base + (i % 5) * 0.25, tf="5m" if step == 300 else "1m", **kw))
        s += step
        i += 1
    return out


def write_csv(path: Path, rows: list[dict], columns=COLUMNS, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = mode == "w" or not path.exists()
    with open(path, mode, encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerows(rows)
    return path


def write_dbn(path: Path, mappings: dict[str, list[tuple[date, date, int]]], records: list[tuple],
              schema: str = "ohlcv-1m") -> Path:
    """生成小型 DBN 文件。mappings: 代码 -> [(起, 止, instrument_id)]；records: (instrument_id, 起始 epoch 秒, o, h, l, c, v)，价格为点数。"""
    import databento_dbn as d
    sch = {"ohlcv-1m": d.Schema.OHLCV_1M, "trades": d.Schema.TRADES}[schema]
    maps = [N(raw_symbol=sym, intervals=[N(start_date=a, end_date=b, symbol=str(iid)) for a, b, iid in ivs])
            for sym, ivs in mappings.items()]
    start = min((r[1] for r in records), default=0) * 1_000_000_000
    meta = d.Metadata(dataset="GLBX.MDP3", start=start, stype_in=d.SType.PARENT, stype_out=d.SType.INSTRUMENT_ID,
                      schema=sch, symbols=["ES.FUT"], mappings=maps)
    fx = lambda p: int(Decimal(str(p)) * 1_000_000_000)   # noqa: E731
    body = b"".join(bytes(d.OHLCVMsg(33, 1, iid, t * 1_000_000_000, fx(o), fx(h), fx(l), fx(c), v))
                    for iid, t, o, h, l, c, v in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(meta.encode() + (body if schema == "ohlcv-1m" else b""))
    return path


class FakeAdapter:
    """collect 测试用的假行情适配器。"""

    name = "webull"

    def __init__(self, rows=None, returned=None, error=None):
        self.rows, self.returned, self.error, self.calls = rows or [], returned, error, []

    def fetch(self, contract_id, timeframe, count, start_utc=None, end_utc=None):
        self.calls.append((contract_id, timeframe, count, start_utc, end_utc))
        if self.error:
            raise self.error
        from trading_agent.contracts import short_symbol
        rows = [r for r in self.rows if (start_utc is None or r["start"] >= start_utc)
                and (end_utc is None or r["start"] <= end_utc)]
        return (self.returned or short_symbol(contract_id)), rows[-count:]


def wb_row(start: int, price: float = 5000.0, volume: int = 10) -> dict:
    p = Decimal(str(price))
    return {"start": start, "open": str(p), "high": str(p + 1), "low": str(p - 1), "close": str(p), "volume": str(volume)}
