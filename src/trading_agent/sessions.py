"""交易日历与时段。RTH 取 XNYS 日历（含提前收盘），ETH 为 Globex 交易日内 RTH 之外的时段。

全部用整数 UTC epoch 秒运算，供逐根 K 线分类使用（数百万次调用）。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
CALENDAR_NAME = "XNYS"
CALENDAR_VERSION = f"{CALENDAR_NAME}/exchange_calendars-{xcals.__version__}"
GLOBEX_OPEN_SEC = 18 * 3600      # 交易日从前一日 18:00 ET 开始
GLOBEX_CLOSE_SEC = 17 * 3600     # 17:00–18:00 ET 为维护时段
OUTSIDE = "OUTSIDE"

_sessions: dict[int, tuple[int, int]] | None = None   # date.toordinal() -> (rth_open, rth_close) UTC 秒
_ordinals: list[int] = []


def _load():
    global _sessions, _ordinals
    if _sessions is None:
        cal = xcals.get_calendar(CALENDAR_NAME)
        sch = cal.schedule
        s = {}
        for day, o, c in zip(sch.index, sch["open"], sch["close"]):
            s[day.date().toordinal()] = (int(o.timestamp()), int(c.timestamp()))
        _sessions = s
        _ordinals = sorted(s)
    return _sessions


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


def et(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, NY)


def iso(epoch: int | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> int:
    """ISO 8601 → UTC epoch 秒。不带时区按 UTC。带小数秒时向上取整（可用时点宁晚勿早）。"""
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    t = dt.timestamp()
    return int(t) if t == int(t) else int(t) + 1


@lru_cache(maxsize=None)
def _offset(utc_ordinal: int) -> int:
    """该 UTC 日期正午时纽约的 UTC 偏移（秒）。夏令时切换在周日凌晨，市场休市，所以整日有效。"""
    d = date.fromordinal(utc_ordinal)
    return int(datetime(d.year, d.month, d.day, 12, tzinfo=UTC).astimezone(NY).utcoffset().total_seconds())


_EPOCH_ORD = date(1970, 1, 1).toordinal()


def classify(epoch: int) -> tuple[date | None, str]:
    """K 线起始时刻 → (交易日, 'RTH'|'ETH'|'OUTSIDE')。"""
    sessions = _load()
    local = epoch + _offset(_EPOCH_ORD + epoch // 86400)
    day_ord = _EPOCH_ORD + local // 86400
    sod = local % 86400
    if GLOBEX_CLOSE_SEC <= sod < GLOBEX_OPEN_SEC:
        return None, OUTSIDE
    g = day_ord + 1 if sod >= GLOBEX_OPEN_SEC else day_ord
    wd = date.fromordinal(g).weekday()
    if wd >= 5 or g > _ordinals[-1] or g < _ordinals[0]:
        return None, OUTSIDE          # 周末，或超出日历覆盖范围
    b = sessions.get(g)
    if b is None:
        # 休市日的 Globex 交易：归入下一个交易日的 ETH 背景。
        nxt = next_trading_day(date.fromordinal(g))
        return nxt, "ETH"
    return date.fromordinal(g), ("RTH" if b[0] <= epoch < b[1] else "ETH")


def is_trading_day(day: date) -> bool:
    return day.toordinal() in _load()


def rth_bounds(day: date) -> tuple[int, int]:
    return _load()[day.toordinal()]


def is_early_close(day: date) -> bool:
    o, c = rth_bounds(day)
    return et(c).time() < time(16, 0)


def eth_bounds(day: date) -> tuple[int, int]:
    """交易日的 Globex 起止：前一日历日 18:00 ET 至当日 17:00 ET。"""
    prev = day - timedelta(days=1)
    start = datetime.combine(prev, time(18, 0), NY)
    end = datetime.combine(day, time(17, 0), NY)
    return ts(start), ts(end)


def next_trading_day(day: date) -> date:
    import bisect
    _load()
    i = bisect.bisect_right(_ordinals, day.toordinal())
    return date.fromordinal(_ordinals[i])


def prev_trading_day(day: date) -> date:
    import bisect
    _load()
    i = bisect.bisect_left(_ordinals, day.toordinal())
    return date.fromordinal(_ordinals[i - 1])


def trading_days(start: date, end: date) -> list[date]:
    import bisect
    _load()
    i = bisect.bisect_left(_ordinals, start.toordinal())
    j = bisect.bisect_right(_ordinals, end.toordinal())
    return [date.fromordinal(o) for o in _ordinals[i:j]]


def expected_rth_slots(day: date, step: int = 300) -> list[int]:
    o, c = rth_bounds(day)
    return list(range(o, c, step))


def trading_day_at(epoch: int) -> date | None:
    """该时刻所属的交易日（落在休市或维护时段时取下一个交易日）。"""
    d, s = classify(epoch)
    if d is not None:
        return d
    return next_trading_day(et(epoch).date() - timedelta(days=1))
