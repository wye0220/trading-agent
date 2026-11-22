"""需要 Webull 凭证并联网。默认不运行：uv run pytest -m webull_live

用来核实 docs/data-contract.md「Webull 来源」里尚未核实的几项：期货代码格式、返回字段、时间戳口径、回看上限、夜盘。
"""
import time

import pytest

from trading_agent import contracts, sessions
from trading_agent.config import load_settings
from trading_agent.sources.webull import WebullBars

pytestmark = pytest.mark.webull_live


def test_fetch_active_es_contract_5m():
    st = load_settings()
    now = int(time.time())
    cid = contracts.active_contract(st.instrument("ES"), sessions.trading_day_at(now), st.roll_rules)
    returned, rows = WebullBars(project_root=st.project_root).fetch(cid, "5m", 1200, None, now)
    assert rows, "Webull 没有返回 ES 期货的 5 分钟线"
    assert contracts.normalize(returned, sessions.et(now).date()) == cid
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts) and all(s % 300 == 0 for s in starts)
    assert {"open", "high", "low", "close", "volume"} <= set(rows[0])
    sess = {sessions.classify(s)[1] for s in starts}
    print(f"\n{cid}: {len(rows)} 根，{sessions.iso(starts[0])} 至 {sessions.iso(starts[-1])}，时段 {sorted(sess)}")
