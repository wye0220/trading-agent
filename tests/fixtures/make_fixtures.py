"""生成 tests/fixtures/ 下的合成（FIXTURE）样本。文件名都含 fixture，导入后带 FIXTURE 标记。

    uv run python tests/fixtures/make_fixtures.py

覆盖：正常读取、冲突、缺 bar、换月错误、夏令时转换、半日市、ETH→RTH 衔接，另有一个小型 DBN。
"""
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import helpers as H  # noqa: E402
from trading_agent import sessions  # noqa: E402


def main():
    d1, d2 = date(2025, 11, 25), date(2025, 11, 26)
    # 正常读取 + ETH→RTH 衔接：夜盘接 RTH，连续两天
    H.write_csv(HERE / "fixture_normal_eth_rth.csv",
                H.eth_bars(d1) + H.rth_bars(d1) + H.eth_bars(d2) + H.rth_bars(d2, base=5010))
    # 缺 bar：RTH 中间缺 10:15
    H.write_csv(HERE / "fixture_missing_bar.csv", H.rth_bars(date(2025, 11, 24), skip=[(10, 15)]))
    # 冲突与坏行：同键内容不同的两行（无取数时刻的来源按两次 revision 处理见 DBN 样本）、OHLC 不一致、不在 tick 上
    s = H.et(date(2025, 11, 20), 9, 30)
    H.write_csv(HERE / "fixture_bad_rows.csv", [
        H.bar(s, 5000), H.bar(s + 300, 5000, high=4999), H.bar(s + 600, 5000.1), H.bar(s + 900, 5000)])
    # 换月错误：roll-v1 指定 2025-12-11 起为 ESH2026，这里只给旧合约
    H.write_csv(HERE / "fixture_wrong_contract.csv", H.rth_bars(date(2025, 12, 11), contract="ESZ2025"))
    # 夏令时转换：切换前后的交易日
    rows = []
    for d in (date(2025, 3, 7), date(2025, 3, 10)):
        rows += H.rth_bars(d, contract="ESH2025")
    for d in (date(2025, 10, 31), date(2025, 11, 3)):
        rows += H.rth_bars(d, contract="ESZ2025")
    H.write_csv(HERE / "fixture_dst.csv", rows)
    # 半日市：2025-11-28 13:00 ET 提前收盘
    H.write_csv(HERE / "fixture_half_day.csv", H.rth_bars(date(2025, 11, 28)))
    # 小型 DBN：单腿 + 价差 + 文件内冲突
    o = sessions.rth_bounds(date(2025, 11, 21))[0]
    recs = [(111, o + 60 * i, 5000 + 0.25 * (i % 4), 5001 + 0.25 * (i % 4), 4999, 5000.5, 5) for i in range(390)]
    recs += [(111, o + 60 * 7, 5005, 5006, 5004, 5005, 9)]              # 与第 8 根同键不同内容
    recs += [(222, o, 1.0, 1.25, 0.75, 1.0, 3)]                         # 价差
    H.write_dbn(HERE / "fixture_small.ohlcv-1m.dbn",
                {"ESZ5": [(date(2025, 1, 1), date(2026, 1, 1), 111)],
                 "ESZ5-ESH6": [(date(2025, 1, 1), date(2026, 1, 1), 222)]}, recs)
    print("\n".join(sorted(p.name for p in HERE.iterdir() if p.name.startswith("fixture_"))))


if __name__ == "__main__":
    main()
