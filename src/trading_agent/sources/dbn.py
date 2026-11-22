"""Databento DBN（ohlcv-1m）只读适配器。本地解码，不联网。"""
from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Iterator

import databento_dbn as dbn

from .. import contracts
from ..bars import OffTickPrice, RawBar, fixed_to_ticks
from .base import Anomaly, UnsupportedSchema, is_fixture_name

SOURCE = "databento"
SUPPORTED_SCHEMA = "ohlcv-1m"
CHUNK = 1 << 22
_EPOCH_ORD = date(1970, 1, 1).toordinal()
_NS = 1_000_000_000


def _open(path: Path):
    f = open(path, "rb")
    if path.name.endswith(".zst"):
        import zstandard
        return zstandard.ZstdDecompressor().stream_reader(f)
    return f


def read_metadata(path: Path) -> dbn.Metadata:
    dec = dbn.DBNDecoder()
    with _open(path) as f:
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                raise UnsupportedSchema("文件里没有 DBN 元数据")
            dec.write(chunk)
            out = dec.decode()
            if out:
                return out[0]


def read(path: Path, instruments: dict, stats: Counter) -> Iterator[RawBar | Anomaly]:
    """逐条产出单腿合约的 1 分钟线。价差合约丢弃并计数；无法映射的记录产出 Anomaly。

    stats 计数：records、spread_records、spread_symbols、unregistered_records。
    """
    meta = read_metadata(path)
    schema = str(meta.schema)
    if schema != SUPPORTED_SCHEMA:
        raise UnsupportedSchema(schema)
    fixture = is_fixture_name(path.name)

    # instrument_id -> [(start_ord, end_ord, raw_symbol)]
    intervals: dict[int, list[tuple[int, int, str]]] = {}
    spread_symbols = set()
    for raw, ivs in meta.mappings.items():
        if contracts.is_spread(raw):
            spread_symbols.add(raw)
        for iv in ivs:
            intervals.setdefault(int(iv["symbol"]), []).append(
                (iv["start_date"].toordinal(), iv["end_date"].toordinal(), raw))
    stats["spread_symbols"] += len(spread_symbols)

    resolved: dict[tuple[int, int], tuple | None] = {}   # (iid, interval start) -> (root, contract_id, tick) | None

    def resolve(iid: int, day_ord: int):
        for s, e, raw in intervals.get(iid, ()):
            if s <= day_ord < e:
                key = (iid, s)
                if key not in resolved:
                    if contracts.is_spread(raw):
                        resolved[key] = ("SPREAD",)
                    else:
                        try:
                            cid = contracts.normalize(raw, date.fromordinal(s))
                            root = contracts.parse(cid)[0]
                            inst = instruments.get(root)
                            resolved[key] = (root, cid, inst.tick_size) if inst else ("UNREGISTERED",)
                        except contracts.ContractError:
                            resolved[key] = None
                return resolved[key]
        return None

    dec = dbn.DBNDecoder()
    first = True
    with _open(path) as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            dec.write(chunk)
            recs = dec.decode()
            if first and recs:
                recs = recs[1:]
                first = False
            for r in recs:
                stats["records"] += 1
                start = r.ts_event // _NS
                iid = r.instrument_id
                hit = resolve(iid, _EPOCH_ORD + start // 86400)
                if hit is None:
                    yield Anomaly("UNMAPPED_INSTRUMENT", SOURCE, timeframe="1m", start_utc=start,
                                  end_utc=start + 60, detail=f"instrument_id={iid}")
                    continue
                if len(hit) == 1:
                    stats["spread_records" if hit[0] == "SPREAD" else "unregistered_records"] += 1
                    continue
                root, cid, tick = hit
                try:
                    o, h, l, c = (fixed_to_ticks(v, tick) for v in (r.open, r.high, r.low, r.close))
                except OffTickPrice as e:
                    yield Anomaly("OFF_TICK_PRICE", SOURCE, root, cid, "1m", start, start + 60, detail=str(e))
                    continue
                yield RawBar(SOURCE, root, cid, "1m", start, o, h, l, c, int(r.volume), None, fixture)
