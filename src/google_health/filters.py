"""Build AIP-160 filter expressions for ``dataPoints.list`` / ``dataPoints.reconcile``.

The Google Health API accepts different filter fields depending on how a data
type is anchored in time. :func:`time_range_filter` picks the right one so you
never have to remember whether it is ``steps.interval.start_time`` or
``weight.sample_time.physical_time``.
"""

from __future__ import annotations

from typing import Optional, Union

from ._time import DateTimeLike, civil_literal, rfc3339, to_date
from .types import DataKind, DataType


def _clause(field: str, start: Optional[str], end: Optional[str]) -> str:
    parts = []
    if start is not None:
        parts.append(f'{field} >= "{start}"')
    if end is not None:
        parts.append(f'{field} < "{end}"')
    if not parts:
        raise ValueError("at least one of start/end is required")
    return " AND ".join(parts)


def time_range_filter(
    data_type: Union[DataType, str],
    start: Optional[DateTimeLike] = None,
    end: Optional[DateTimeLike] = None,
    *,
    civil: bool = False,
) -> str:
    """Return a filter selecting points in ``[start, end)``.

    :param civil: interpret ``start``/``end`` as the user's local wall-clock time
        (``civil_start_time`` fields) instead of absolute UTC instants.
    """
    dtype = DataType(data_type)
    kind = dtype.kind
    name = dtype.snake

    if kind is DataKind.ROLLUP_ONLY:
        raise ValueError(f"{dtype.value} has no raw data points; use roll_up/daily_roll_up")

    if kind is DataKind.DAILY:
        return _clause(
            f"{name}.date",
            to_date(start).isoformat() if start is not None else None,
            to_date(end).isoformat() if end is not None else None,
        )

    conv = civil_literal if civil else rfc3339
    s = conv(start) if start is not None else None
    e = conv(end) if end is not None else None

    if kind is DataKind.SAMPLE:
        field = f"{name}.sample_time.{'civil_time' if civil else 'physical_time'}"
        return _clause(field, s, e)

    if kind is DataKind.INTERVAL:
        field = f"{name}.interval.{'civil_start_time' if civil else 'start_time'}"
        return _clause(field, s, e)

    # Sessions have per-type quirks.
    if dtype is DataType.SLEEP:
        field = f"sleep.interval.{'civil_end_time' if civil else 'end_time'}"
        return _clause(field, s, e)
    if dtype is DataType.ELECTROCARDIOGRAM:
        if civil:
            raise ValueError("electrocardiogram only supports physical start_time filters")
        if e is not None:
            raise ValueError("electrocardiogram only supports a lower bound (start)")
        return _clause("electrocardiogram.interval.start_time", s, None)
    # Other sessions (exercise, hydration-log, nutrition-log, ...) only support
    # filtering on civil start time, so bounds are always sent as wall-clock values.
    s = civil_literal(start) if start is not None else None
    e = civil_literal(end) if end is not None else None
    return _clause(f"{name}.interval.civil_start_time", s, e)
