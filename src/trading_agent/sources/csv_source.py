"""数据合同 CSV 的只读适配器。Webull 采集结果和合成样本都用这个格式。"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Iterator

from .. import contracts
from ..bars import TIMEFRAME_SECONDS, OffTickPrice, RawBar, decimal_to_ticks
from ..sessions import parse_utc
from .base import Anomaly, RejectedFile, is_fixture_name

REQUIRED = ("instrument_root", "contract_id", "timeframe", "bar_start_utc",
            "open", "high", "low", "close", "volume", "source")
OPTIONAL = ("fetched_at_utc", "fixture", "bar_end_utc")
COLUMNS = REQUIRED + ("bar_end_utc", "fetched_at_utc")     # collect 写出的列顺序


def split_lines(data: bytes) -> list[bytes]:
    """按行切开（保留换行符），供增量导入计算前缀摘要。"""
    return data.splitlines(keepends=True)


def read(lines: list[bytes], name: str, instruments: dict, start_row: int = 0) -> Iterator[RawBar | Anomaly]:
    """lines[0] 是表头。从第 start_row 条数据行（0 起）开始产出。缺必需列则抛 RejectedFile。"""
    if not lines:
        raise RejectedFile("空文件")
    header = next(csv.reader(io.StringIO(lines[0].decode("utf-8-sig"))), [])
    header = [h.strip() for h in header]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise RejectedFile("缺少必需列：" + "、".join(missing))
    name_fixture = is_fixture_name(name)
    body = b"".join(lines[1 + start_row:]).decode("utf-8")
    for i, row in enumerate(csv.DictReader(io.StringIO(body), fieldnames=header), start=start_row):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        src = (row.get("source") or "").strip()
        tf = (row.get("timeframe") or "").strip()
        try:
            root = row["instrument_root"].strip()
            inst = instruments.get(root)
            if inst is None:
                raise ValueError(f"品种未登记：{root}")
            if tf not in ("1m", "5m"):
                raise ValueError(f"不支持的周期：{tf!r}")
            if not src:
                raise ValueError("source 为空")
            start = parse_utc(row["bar_start_utc"])
            if start % TIMEFRAME_SECONDS[tf]:
                raise ValueError("bar_start_utc 没有对齐到周期边界")
            cid = contracts.normalize(row["contract_id"].strip(),
                                      datetime.fromtimestamp(start, timezone.utc).date())
            if contracts.parse(cid)[0] != root:
                raise ValueError(f"contract_id {cid} 不属于 {root}")
            vol = int(float(row["volume"] or 0))
            fetched = parse_utc(row["fetched_at_utc"]) if (row.get("fetched_at_utc") or "").strip() else None
            fixture = name_fixture or (row.get("fixture") or "").strip().lower() in ("1", "true", "yes", "fixture")
        except (ValueError, KeyError, AttributeError, contracts.ContractError) as e:
            yield Anomaly("BAD_ROW", src or None, detail=f"{name} 第 {i + 2} 行：{e}")
            continue
        try:
            o, h, l, c = (decimal_to_ticks(row[k], inst.tick_size) for k in ("open", "high", "low", "close"))
        except OffTickPrice as e:
            yield Anomaly("OFF_TICK_PRICE", src, root, cid, tf, start, start + TIMEFRAME_SECONDS[tf], detail=str(e))
            continue
        yield RawBar(src, root, cid, tf, start, o, h, l, c, vol, fetched, fixture)
