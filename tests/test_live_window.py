"""截止日之后的实时行情放行（config/live_data.yaml）。"""
from datetime import date

import pytest

import helpers as H
import test_research as TR
from conftest import Env
from trading_agent import live_window as LW
from trading_agent.config import ConfigError
from trading_agent.research import splits

DAYS = TR.days_from(TR.START, 8)
CUT = DAYS[3]                       # 截止日
LIVE_FROM = DAYS[6]                 # 放行首日


def window(**kw):
    return LW.parse({"sources": ["webull"], "from_day": {"ES": LIVE_FROM.isoformat(), **kw}})


@pytest.fixture
def lenv(tmp_path, monkeypatch):
    monkeypatch.setattr(LW, "load", lambda project_root: window())
    e = Env(tmp_path, dev_visible_through=CUT)
    yield e
    e.con.close()


def rows(day, source):
    return H.rth_bars(day, fixture=False, source=source, contract="ESZ2025")


def days_in(e, source):
    return [r[0] for r in e.con.execute("SELECT DISTINCT trading_day FROM bars WHERE source=? AND derived=0"
                                        " ORDER BY 1", (source,))]


def test_parse_and_allows():
    w = window()
    assert w.allows("webull", "ES", LIVE_FROM.isoformat()) and w.allows("webull", "ES", "2099-01-01")
    assert not w.allows("databento", "ES", LIVE_FROM.isoformat())
    assert not w.allows("webull", "ES", DAYS[5].isoformat()) and not w.allows("webull", "NQ", "2099-01-01")
    assert LW.parse({}) == LW.EMPTY and not LW.EMPTY.allows("webull", "ES", "2099-01-01")
    with pytest.raises(ConfigError):
        LW.parse({"sources": ["webull"], "from_day": {"ES": "2026-13-01"}})


def test_ingest_admits_only_live_source_from_first_day(lenv):
    data = []
    for d in DAYS:
        data += rows(d, "databento")
    for d in DAYS[5:]:
        data += rows(d, "webull")
    H.write_csv(lenv.root / "mixed.csv", data)
    s = lenv.ingest()
    # 截止日之前照常；放行首日起只有 webull 进库；Databento 的同一天和 webull 首日之前的照旧封存
    assert days_in(lenv, "databento") == [d.isoformat() for d in DAYS[:4]]
    assert days_in(lenv, "webull") == [d.isoformat() for d in DAYS[6:]]
    assert s["live_after_cutoff"] == 2 * len(rows(DAYS[6], "webull"))
    held = {(r[0], r[1]) for r in lenv.con.execute("SELECT DISTINCT source, trading_day FROM withheld_coverage")}
    assert ("webull", DAYS[5].isoformat()) in held and ("databento", DAYS[7].isoformat()) in held
    assert ("webull", DAYS[6].isoformat()) not in held
    assert s["live_window"]["from_day"] == {"ES": LIVE_FROM.isoformat()}
    # 交易日状态里有放行日，可供盘中扫描
    td = {r[0] for r in lenv.con.execute("SELECT trading_day FROM trading_days WHERE instrument_root='ES'")}
    assert LIVE_FROM.isoformat() in td and DAYS[5].isoformat() not in td
    # 再导入一次（会执行截止日清理）：放行的实时行情不被移出
    lenv.ingest()
    assert days_in(lenv, "webull") == [d.isoformat() for d in DAYS[6:]]


def test_split_registry_ignores_live_days(lenv):
    data = []
    for d in DAYS[:4]:
        data += rows(d, "databento")
    for d in DAYS[4:6]:
        data += rows(d, "databento")              # 封存
    for d in DAYS[6:]:
        data += rows(d, "webull")                 # 放行
    H.write_csv(lenv.root / "m.csv", data)
    lenv.ingest()
    c = TR.cfg(**{"splits.forward_validation_start": DAYS[2].isoformat(), "splits.warmup_trading_days": 0,
                  "splits.capsule_valid_days": 1})
    reg = splits.build(lenv.settings, lenv.con, "ES", c)    # 库里有截止日之后的放行数据也不拒绝
    seen = {d[0] for s in reg["segments"] for d in s["days"]}
    assert not any(d >= LIVE_FROM.isoformat() for d in seen)
    assert {DAYS[4].isoformat(), DAYS[5].isoformat()} <= seen          # 封存的两天成了胶囊
    assert reg["definition"]["last_visible_day"] == CUT.isoformat()


def test_conflict_with_sealed_days_disables_root(lenv, monkeypatch):
    data = []
    for d in DAYS[:7]:
        data += rows(d, "databento")
    H.write_csv(lenv.root / "a.csv", data)
    lenv.ingest()
    c = TR.cfg(**{"splits.forward_validation_start": DAYS[2].isoformat(), "splits.warmup_trading_days": 0,
                  "splits.capsule_valid_days": 1})
    monkeypatch.setattr(LW, "load", lambda project_root: LW.EMPTY)
    splits.register(lenv.settings, lenv.con, "ES", c)        # 封存到 DAYS[6]
    w = LW.effective(lenv.con, lenv.settings.project_root, window())
    assert w.from_day == {} and "不放行" in w.conflicts[0]
    monkeypatch.setattr(LW, "load", lambda project_root: window())
    H.write_csv(lenv.root / "b.csv", rows(DAYS[7], "webull"))
    s = lenv.ingest()
    assert days_in(lenv, "webull") == [] and s["live_window"]["conflicts"]
