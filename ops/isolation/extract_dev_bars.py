"""把 Databento 原始文件里截止日（含）之前的 K 线抽出来，存成数据合同格式的 CSV，留在作者侧。

隔离之后，含截止日之后数据的原始文件整份交给评测身份（评测窗口前还要预热数据）。作者侧靠这份 CSV
仍能从原始数据重建运行库。只读原文件；输出文件已存在时拒绝覆盖。

用法：uv run python ops/isolation/extract_dev_bars.py <原始 .dbn 文件> <输出 .csv>
"""
import csv
import sys
from collections import Counter
from pathlib import Path

from trading_agent import sessions
from trading_agent.config import load_settings
from trading_agent.sources import dbn
from trading_agent.sources.csv_source import COLUMNS


def main(src: str, out: str) -> int:
    settings = load_settings()
    cut = settings.dev_visible_through.isoformat()
    dst = Path(out)
    if dst.exists():
        print(f"拒绝：{dst} 已存在")
        return 2
    dst.parent.mkdir(parents=True, exist_ok=True)
    stats, kept, dropped = Counter(), 0, 0
    tmp = dst.with_suffix(".partial")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(COLUMNS)
        for b in dbn.read(Path(src), settings.instruments, stats):
            if not hasattr(b, "bar_start_utc") or not hasattr(b, "open"):
                continue                                    # 异常记录：入库时同样会被记下，这里不需要
            day, _ = sessions.classify(b.bar_start_utc)
            if day is None or day.isoformat() > cut:
                dropped += 1
                continue
            tick = settings.instrument(b.instrument_root).tick_size
            w.writerow([b.instrument_root, b.contract_id, b.timeframe, sessions.iso(b.bar_start_utc),
                        *(str(v * tick) for v in (b.open, b.high, b.low, b.close)), b.volume, b.source,
                        sessions.iso(b.bar_end_utc), ""])
            kept += 1
    tmp.rename(dst)
    print(f"截止 {cut}（含）之前 {kept:,} 根，之后 {dropped:,} 根没有写出。记录数 {stats['records']:,}。输出 {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]) if len(sys.argv) == 3 else (print(__doc__) or 2))
