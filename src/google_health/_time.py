"""Time conversion helpers between Python objects and Google API wire formats."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple, Union

DateLike = Union[dt.date, str]
DateTimeLike = Union[dt.datetime, dt.date, str]
DurationLike = Union[dt.timedelta, int, float, str]

UTC = dt.timezone.utc


def to_date(value: DateLike) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value == "today":
        return dt.date.today()
    return dt.date.fromisoformat(value[:10])


def to_utc_datetime(value: DateTimeLike) -> dt.datetime:
    """Coerce to an aware UTC datetime. Naive values are treated as UTC."""
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        value = dt.datetime.fromisoformat(s) if "T" in s or " " in s else dt.date.fromisoformat(s)
    if not isinstance(value, dt.datetime):
        value = dt.datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def rfc3339(value: DateTimeLike) -> str:
    d = to_utc_datetime(value)
    if d.microsecond:
        return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def civil_literal(value: DateTimeLike) -> str:
    """ISO-8601 ``YYYY-MM-DD[THH:mm:ss]`` literal without offset (civil time)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None, microsecond=0).isoformat()
    return value.isoformat()


def duration(value: DurationLike) -> str:
    """Encode a protobuf ``Duration`` (``"3600s"``)."""
    if isinstance(value, str):
        value = parse_window(value)
    if isinstance(value, dt.timedelta):
        seconds: float = value.total_seconds()
    else:
        seconds = float(value)
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return f"{int(seconds)}s" if seconds == int(seconds) else f"{seconds:.9f}".rstrip("0") + "s"


_WINDOW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr|d)?\s*$")
_WINDOW_UNITS = {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600,
                 "d": 86400}


def parse_window(value: str) -> dt.timedelta:
    """Parse ``"900s"``, ``"15m"``, ``"15min"``, ``"1h"`` or ``"1d"``."""
    m = _WINDOW_RE.match(value)
    if not m:
        raise ValueError(f"invalid window {value!r}; use e.g. '900s', '15m', '1h'")
    td = dt.timedelta(seconds=float(m.group(1)) * _WINDOW_UNITS[m.group(2)])
    if td <= dt.timedelta(0):
        raise ValueError("window must be positive")
    return td


_DUR_RE = re.compile(r"^(-?\d+(?:\.\d+)?)s$")


def parse_duration(value: Optional[str]) -> Optional[dt.timedelta]:
    """Parse a protobuf Duration (``"-14400s"``) or ``"+03:00"`` style offset."""
    if value is None or value == "":
        return None
    m = _DUR_RE.match(value)
    if m:
        return dt.timedelta(seconds=float(m.group(1)))
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", value)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        return sign * dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    raise ValueError(f"unrecognised duration/offset: {value!r}")


def date_message(d: DateLike) -> Dict[str, int]:
    d = to_date(d)
    return {"year": d.year, "month": d.month, "day": d.day}


def civil_datetime_message(value: DateTimeLike) -> Dict[str, Any]:
    if isinstance(value, dt.datetime):
        msg: Dict[str, Any] = {"date": date_message(value.date())}
        msg["time"] = {"hours": value.hour, "minutes": value.minute, "seconds": value.second}
        return msg
    return {"date": date_message(value)}


def parse_date_message(msg: Optional[Mapping[str, Any]]) -> Optional[dt.date]:
    if not msg:
        return None
    return dt.date(int(msg["year"]), int(msg.get("month", 1)), int(msg.get("day", 1)))


def parse_civil_datetime(msg: Optional[Mapping[str, Any]]) -> Optional[dt.datetime]:
    """Parse a ``CivilDateTime`` message into a naive (local wall-clock) datetime."""
    if not msg:
        return None
    d = parse_date_message(msg.get("date"))
    if d is None:
        return None
    t = msg.get("time") or {}
    return dt.datetime(
        d.year,
        d.month,
        d.day,
        int(t.get("hours", 0)),
        int(t.get("minutes", 0)),
        int(t.get("seconds", 0)),
        int(t.get("nanos", 0)) // 1000,
    )


def parse_timestamp(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    s = value.replace("Z", "+00:00")
    # Python < 3.11 cannot parse more than 6 fractional digits.
    m = re.match(r"^(.*?\.\d{6})\d+(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    return dt.datetime.fromisoformat(s)


def chunk_dates(start: dt.date, end: dt.date, max_days: int) -> Iterator[Tuple[dt.date, dt.date]]:
    """Split ``[start, end)`` into consecutive windows of at most ``max_days``."""
    cur = start
    step = dt.timedelta(days=max_days)
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


def chunk_datetimes(
    start: dt.datetime, end: dt.datetime, max_span: dt.timedelta, align: dt.timedelta
) -> Iterator[Tuple[dt.datetime, dt.datetime]]:
    """Split ``[start, end)`` into windows ≤ ``max_span`` whose length is a multiple of
    ``align`` (the roll-up window) so buckets are never cut in the middle."""
    if align > max_span:
        raise ValueError("roll-up window is larger than the maximum allowed range")
    n = max(1, int(max_span / align))
    span = align * n
    cur = start
    while cur < end:
        nxt = min(cur + span, end)
        yield cur, nxt
        cur = nxt
