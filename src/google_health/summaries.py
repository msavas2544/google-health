"""Helpers that turn raw roll-up responses into simple Python values.

The API returns int64 fields as strings and nests every value under a
type-specific key. :func:`rollup_value` flattens the common cases::

    >>> rollup_value("steps", {"steps": {"countSum": "8421"}})
    8421.0
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union

from ._time import parse_civil_datetime, parse_timestamp
from .types import DataType

#: data type -> (rollup key path, multiplier to convert to the documented unit)
_SCALARS: Dict[DataType, Tuple[Tuple[str, ...], float, str]] = {
    DataType.STEPS: (("steps", "countSum"), 1, "steps"),
    DataType.DISTANCE: (("distance", "millimetersSum"), 1e-3, "m"),
    DataType.FLOORS: (("floors", "countSum"), 1, "floors"),
    DataType.ALTITUDE: (("altitude", "gainMillimetersSum"), 1e-3, "m"),
    DataType.TOTAL_CALORIES: (("totalCalories", "kcalSum"), 1, "kcal"),
    DataType.ACTIVE_ENERGY_BURNED: (("activeEnergyBurned", "kcalSum"), 1, "kcal"),
    DataType.WEIGHT: (("weight", "weightGramsAvg"), 1e-3, "kg"),
    DataType.BODY_FAT: (("bodyFat", "bodyFatPercentageAvg"), 1, "%"),
    DataType.HEART_RATE: (("heartRate", "beatsPerMinuteAvg"), 1, "bpm"),
    DataType.BLOOD_GLUCOSE: (("bloodGlucose", "bloodGlucoseMilligramsPerDeciliterAvg"), 1,
                             "mg/dL"),
    DataType.CORE_BODY_TEMPERATURE: (("coreBodyTemperature", "temperatureCelsiusAvg"), 1, "°C"),
    DataType.RUN_VO2_MAX: (("runVo2Max", "rateAvg"), 1, "ml/kg/min"),
    DataType.SWIM_LENGTHS_DATA: (("swimLengthsData", "strokeCountSum"), 1, "strokes"),
}


def _seconds(v: Any) -> float:
    if isinstance(v, str) and v.endswith("s"):
        return float(v[:-1])
    return float(v)


def _azm(p: Mapping[str, Any]) -> Optional[float]:
    v = p.get("activeZoneMinutes")
    if not v:
        return None
    return float(sum(int(v.get(k, 0) or 0) for k in (
        "sumInFatBurnHeartZone", "sumInCardioHeartZone", "sumInPeakHeartZone")))


def _sedentary(p: Mapping[str, Any]) -> Optional[float]:
    v = (p.get("sedentaryPeriod") or {}).get("durationSum")
    return None if v is None else _seconds(v) / 60.0


_SPECIAL: Dict[DataType, Tuple[Callable[[Mapping[str, Any]], Optional[float]], str]] = {
    DataType.ACTIVE_ZONE_MINUTES: (_azm, "min"),
    DataType.SEDENTARY_PERIOD: (_sedentary, "min"),
}


def rollup_unit(data_type: Union[DataType, str]) -> Optional[str]:
    d = DataType(data_type)
    if d in _SCALARS:
        return _SCALARS[d][2]
    if d in _SPECIAL:
        return _SPECIAL[d][1]
    return None


def rollup_value(data_type: Union[DataType, str], point: Mapping[str, Any]) -> Optional[float]:
    """Extract the headline number from a (daily) roll-up point, in SI-ish units:
    metres, kilograms, kcal, bpm, minutes. ``None`` if the window had no data."""
    d = DataType(data_type)
    if d in _SPECIAL:
        return _SPECIAL[d][0](point)
    if d not in _SCALARS:
        raise ValueError(f"no scalar extractor for {d.value}; read the raw roll-up instead")
    (outer, inner), mult, _ = _SCALARS[d]
    v = (point.get(outer) or {}).get(inner)
    if v is None:
        return None
    return float(v) * mult


@dataclass(frozen=True)
class DailyValue:
    date: dt.date
    value: Optional[float]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class WindowValue:
    start: dt.datetime
    end: dt.datetime
    value: Optional[float]
    raw: Mapping[str, Any]


def daily_value(data_type: Union[DataType, str], point: Mapping[str, Any]) -> DailyValue:
    start = parse_civil_datetime(point.get("civilStartTime"))
    if start is None:
        raise ValueError("not a daily roll-up point (missing civilStartTime)")
    return DailyValue(start.date(), rollup_value(data_type, point), point)


def window_value(data_type: Union[DataType, str], point: Mapping[str, Any]) -> WindowValue:
    s, e = parse_timestamp(point.get("startTime")), parse_timestamp(point.get("endTime"))
    if s is None or e is None:
        raise ValueError("not a roll-up point (missing startTime/endTime)")
    return WindowValue(s, e, rollup_value(data_type, point), point)
