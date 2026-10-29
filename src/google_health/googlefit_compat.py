"""Migration layer for code written against the Google Fit REST API.

The Google Fit APIs are deprecated in 2026 and have not accepted new sign-ups
since 1 May 2024. Server-side apps are pointed at the Google Health API. This
module answers the most used Fit REST calls with Google Health API data and
returns the legacy Fit JSON shapes, so existing parsing code keeps working::

    from google_health import HealthClient
    from google_health.googlefit_compat import GoogleFit

    fit = GoogleFit(HealthClient(credentials))

    # was: service.users().dataset().aggregate(userId="me", body=body).execute()
    resp = fit.users().dataset().aggregate(userId="me", body={
        "aggregateBy": [{"dataTypeName": "com.google.step_count.delta"}],
        "bucketByTime": {"durationMillis": 86400000},
        "startTimeMillis": 1761696000000,
        "endTimeMillis": 1762300800000,
    }).execute()
    resp["bucket"][0]["dataset"][0]["point"][0]["value"][0]["intVal"]

Supported: ``dataset:aggregate`` (``bucketByTime`` with ``durationMillis`` or
``period``, ``bucketBySession``, ``bucketByActivitySegment`` and, for additive
types, ``bucketByActivityType``), ``dataSources.list``/``get``,
``dataSources.datasets.get`` and ``sessions.list``. Writes are not supported.

Caveats: values come from Google Health API aggregation and de-duplication, so
they can differ from what Fit returned. Fit OAuth tokens and scopes do not carry
over; users must consent to the Google Health API scopes. This layer was written
against the published Fit and Google Health API references and is covered by
mocked tests, not by a live Fit-to-Health comparison.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ._time import parse_duration, parse_timestamp
from .client import HealthClient
from .types import DataKind, DataType

UTC = dt.timezone.utc
Values = List[Dict[str, Any]]
Bucket = Tuple[dt.datetime, dt.datetime]

#: mg/dL -> mmol/L for glucose (molar mass 180.16 g/mol).
MGDL_PER_MMOLL = 18.016

SLEEP_ACTIVITY = 72

#: Google Health ``exerciseType`` -> Google Fit activity type integer.
EXERCISE_TO_FIT: Dict[str, int] = {
    "BIKING": 1, "OUTDOOR_BIKE": 16, "ELECTRIC_BIKE": 1, "AEROBIC_WORKOUT": 9,
    "BADMINTON": 10, "BASEBALL": 11, "BASKETBALL": 12, "HAND_CYCLING": 14,
    "MOUNTAIN_BIKE": 15, "SPINNING": 17, "STATIONARY_BIKE": 18, "ASSAULT_BIKE": 18,
    "BOXING": 20, "CALISTHENICS": 21, "CIRCUIT_TRAINING": 22, "CRICKET": 23,
    "DANCING": 24, "BALLET": 24, "BALLROOM_DANCE": 24, "HIP_HOP": 24, "JAZZ_DANCE": 24,
    "MODERN_DANCE": 24, "TANGO": 24, "BREAKDANCING": 24, "ELLIPTICAL": 25, "FENCING": 26,
    "FOOTBALL_AMERICAN": 27, "FOOTBALL_AUSTRALIAN": 28, "SOCCER": 29,
    "FRISBEE_PLAYING_GENERAL": 30, "ULTIMATE_FRISBEE": 30, "GARDENING": 31, "GOLF": 32,
    "GYMNASTICS": 33, "HANDBALL": 34, "HIKING": 35, "HOCKEY": 36, "FIELD_HOCKEY": 36,
    "EQUESTRIAN_SPORTS": 37, "HOUSEHOLD_CHORES": 38, "CLEANING": 38, "JUMPING_ROPE": 39,
    "KAYAKING": 40, "CANOEING": 40, "KICKBOXING": 42, "KITESURFING": 43,
    "MARTIAL_ARTS": 44, "KARATE": 44, "TAEKWONDO": 44, "JIU_JITSU": 44, "MUAY_THAI": 44,
    "MEDITATE": 45, "PARAGLIDING": 48, "PILATES": 49, "POLO": 50, "RACQUETBALL": 51,
    "ROCK_CLIMBING": 52, "CLIMBING": 52, "INDOOR_CLIMBING": 52, "ROWING": 53,
    "ROWING_MACHINE": 54, "RUGBY": 55, "RUNNING": 8, "TRAIL_RUN": 8, "INCLINE_RUN": 8,
    "TREADMILL": 58, "SAILING": 59, "SCUBA_DIVING": 60, "SKATEBOARDING": 61,
    "SKATING": 62, "ROLLER_SKATING": 62, "ROLLERBLADING": 64, "SKIING": 65,
    "CROSS_COUNTRY_SKI": 67, "SNOWBOARDING": 73, "SNOWMOBILING": 74, "SNOWSHOEING": 75,
    "SQUASH": 76, "STAIRCLIMBER": 78, "PADDLEBOARDING": 79, "STRENGTH_TRAINING": 80,
    "FUNCTIONAL_STRENGTH_TRAINING": 80, "FREE_WEIGHTS": 80, "WEIGHT_MACHINES": 80,
    "WEIGHTS": 80, "BODY_WEIGHT": 80, "RESISTANCE_BANDS": 80, "SURFING": 81,
    "SWIMMING": 82, "SWIMMING_POOL": 83, "SWIMMING_OPEN_WATER": 84, "TABLE_TENNIS": 85,
    "TENNIS": 87, "VOLLEYBALL": 89, "VOLLEYBALL_BEACH": 90, "WAKEBOARDING": 92,
    "WALKING": 7, "INCLINE_WALK": 7, "WALK_WITH_WEIGHTS": 7, "POWER_WALKING": 93,
    "NORDIC_WALKING": 94, "TREADMILL_WALK": 95, "WATER_POLO": 96, "WEIGHTLIFTING": 97,
    "POWERLIFTING": 97, "WHEELCHAIR": 98, "WINDSURFING": 99, "YOGA": 100,
    "YOGA_BIKRAM": 100, "YOGA_HATHA": 100, "YOGA_POWER": 100, "YOGA_VINYASA": 100,
    "ZUMBA": 101, "DIVING": 102, "ICE_SKATING": 104, "CURLING": 106, "CROSSFIT": 113,
    "HIIT": 114, "TABATA_WORKOUT": 114, "INTERVAL_WORKOUT": 115, "STROLLER_WALK": 116,
    "ARCHERY": 119, "SOFTBALL": 120,
}
FIT_OTHER = 108

#: Google Health sleep stage -> Fit ``sleep_segment_type``.
SLEEP_STAGE_TO_FIT = {"AWAKE": 1, "RESTLESS": 1, "ASLEEP": 2, "LIGHT": 4, "DEEP": 5, "REM": 6}
FIT_OUT_OF_BED = 3

_MEAL_TO_FIT = {"BREAKFAST": 1, "LUNCH": 2, "DINNER": 3, "SNACK": 4}

#: Google Health nutrient -> (Fit nutrient key, factor from grams).
_NUTRIENTS: Dict[str, Tuple[str, float]] = {
    "CARBOHYDRATES": ("carbs.total", 1), "PROTEIN": ("protein", 1),
    "DIETARY_FIBER": ("dietary_fiber", 1), "SUGAR": ("sugar", 1),
    "SATURATED_FAT": ("fat.saturated", 1), "UNSATURATED_FAT": ("fat.unsaturated", 1),
    "POLYUNSATURATED_FAT": ("fat.polyunsaturated", 1),
    "MONOUNSATURATED_FAT": ("fat.monounsaturated", 1), "TRANS_FAT": ("fat.trans", 1),
    "CHOLESTEROL": ("cholesterol", 1000), "SODIUM": ("sodium", 1000),
    "POTASSIUM": ("potassium", 1000), "VITAMIN_C": ("vitamin_c", 1000),
    "CALCIUM": ("calcium", 1000), "IRON": ("iron", 1000),
}

#: Fit data types with no Google Health API equivalent (or not covered here).
UNSUPPORTED = (
    "com.google.calories.bmr", "com.google.speed", "com.google.step_count.cadence",
    "com.google.cycling.pedaling.cadence", "com.google.cycling.wheel_revolution.rpm",
    "com.google.power.sample", "com.google.location.sample", "com.google.location.bounding_box",
    "com.google.blood_pressure", "com.google.body.temperature", "com.google.cervical_mucus",
    "com.google.cervical_position", "com.google.menstruation", "com.google.ovulation_test",
    "com.google.vaginal_spotting", "com.google.activity.exercise", "com.google.internal.goal",
)


class FitCompatError(ValueError):
    """The request uses a Fit feature this layer cannot reproduce."""


# ------------------------------------------------------------------------- values
def _i(v: Any) -> Dict[str, Any]:
    return {"intVal": int(round(float(v))), "mapVal": []}


def _f(v: Any) -> Dict[str, Any]:
    return {"fpVal": float(v), "mapVal": []}


def _s(v: Any) -> Dict[str, Any]:
    return {"stringVal": str(v), "mapVal": []}


def _m(entries: Mapping[str, float]) -> Dict[str, Any]:
    return {"mapVal": [{"key": k, "value": {"fpVal": v}} for k, v in entries.items()]}


def _num(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _get(d: Mapping[str, Any], *path: str) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def _nutrients(log: Mapping[str, Any], *, rollup: bool) -> Dict[str, float]:
    kcal = _get(log, "energy", "kcalSum" if rollup else "kcal")
    grams = "gramsSum" if rollup else "grams"
    out: Dict[str, float] = {}
    if kcal is not None:
        out["calories"] = float(kcal)
    fat = _get(log, "totalFat", grams)
    if fat is not None:
        out["fat.total"] = float(fat)
    carbs = _get(log, "totalCarbohydrate", grams)
    if carbs is not None:
        out["carbs.total"] = float(carbs)
    for n in log.get("nutrients") or []:
        key = _NUTRIENTS.get(str(n.get("nutrient")))
        g = _get(n, "quantity", grams)
        if key and g is not None:
            out[key[0]] = out.get(key[0], 0.0) + float(g) * key[1]
    return out


# ------------------------------------------------------------------- type table
@dataclass(frozen=True)
class FitType:
    name: str
    health: DataType
    #: Fit data type returned by ``dataset:aggregate``.
    aggregate_name: str
    #: Fit dataType.field descriptors (name, format).
    fields: Tuple[Tuple[str, str], ...]
    #: "rollup" (server roll-up), "sample" (client-side avg/max/min of samples),
    #: "activity" (exercise sessions) or "sleep" (dataset/sessions only).
    strategy: str
    stream: str
    #: raw DataPoint -> Fit value list (None = skip the point).
    from_point: Optional[Callable[[Mapping[str, Any]], Optional[Values]]] = None
    #: roll-up point -> Fit aggregate value list (None = no data in the window).
    from_rollup: Optional[Callable[[Mapping[str, Any]], Optional[Values]]] = None
    #: True if aggregate values can be summed across buckets.
    additive: bool = False


def _sum_rollup(outer: str, inner: str, conv: Callable[[Any], Dict[str, Any]],
                scale: float = 1.0) -> Callable[[Mapping[str, Any]], Optional[Values]]:
    def f(p: Mapping[str, Any]) -> Optional[Values]:
        v = _get(p, outer, inner)
        return None if v is None else [conv(float(v) * scale)]
    return f


def _active_minutes_rollup(p: Mapping[str, Any]) -> Optional[Values]:
    rows = _get(p, "activeMinutes", "activeMinutesRollupByActivityLevel")
    if not rows:
        return None
    return [_i(sum(int(r.get("activeMinutesSum", 0) or 0) for r in rows))]


def _heart_minutes_rollup(p: Mapping[str, Any]) -> Optional[Values]:
    v = p.get("activeZoneMinutes")
    if not v:
        return None
    fat = int(v.get("sumInFatBurnHeartZone", 0) or 0)
    hi = int(v.get("sumInCardioHeartZone", 0) or 0) + int(v.get("sumInPeakHeartZone", 0) or 0)
    # Cardio/peak minutes earn 2 points each, like Fit Heart Points.
    return [_f(fat + hi), _i(fat + hi / 2)]


def _heart_rate_rollup(p: Mapping[str, Any]) -> Optional[Values]:
    hr = p.get("heartRate") or {}
    if hr.get("beatsPerMinuteAvg") is None:
        return None
    avg = float(hr["beatsPerMinuteAvg"])
    return [_f(avg), _f(_num(hr.get("beatsPerMinuteMax")) or avg),
            _f(_num(hr.get("beatsPerMinuteMin")) or avg)]


def _hydration_rollup(p: Mapping[str, Any]) -> Optional[Values]:
    ml = _get(p, "hydrationLog", "amountConsumed", "millilitersSum")
    return None if ml is None else [_f(float(ml) / 1000.0)]


def _nutrition_rollup(p: Mapping[str, Any]) -> Optional[Values]:
    log = p.get("nutritionLog")
    if not log:
        return None
    return [_m(_nutrients(log, rollup=True)), _i(0)]


def _sample(key: str, field: str, scale: float = 1.0
            ) -> Callable[[Mapping[str, Any]], Optional[Values]]:
    def f(p: Mapping[str, Any]) -> Optional[Values]:
        v = _get(p, key, field)
        return None if v is None else [_f(float(v) * scale)]
    return f


def _interval(key: str, field: str, conv: Callable[[Any], Dict[str, Any]],
              scale: float = 1.0) -> Callable[[Mapping[str, Any]], Optional[Values]]:
    def f(p: Mapping[str, Any]) -> Optional[Values]:
        v = _get(p, key, *field.split("."))
        return None if v is None else [conv(float(v) * scale)]
    return f


def _nutrition_point(p: Mapping[str, Any]) -> Optional[Values]:
    log = p.get("nutritionLog")
    if not log:
        return None
    return [_m(_nutrients(log, rollup=False)), _i(_MEAL_TO_FIT.get(str(log.get("mealType")), 0)),
            _s(log.get("foodDisplayName") or log.get("food") or "")]


def _exercise_point(p: Mapping[str, Any]) -> Optional[Values]:
    ex = p.get("exercise")
    if not ex:
        return None
    return [_i(EXERCISE_TO_FIT.get(str(ex.get("exerciseType")), FIT_OTHER))]


_GMS = "derived:{}:com.google.android.gms:{}"


def _ft(name: str, health: DataType, agg: str, fields: Sequence[Tuple[str, str]],
        strategy: str, stream: str, **kw: Any) -> FitType:
    return FitType(name, health, agg, tuple(fields), strategy, _GMS.format(name, stream), **kw)


_SUMMARY3 = (("average", "floatPoint"), ("max", "floatPoint"), ("min", "floatPoint"))

FIT_TYPES: Dict[str, FitType] = {t.name: t for t in (
    _ft("com.google.step_count.delta", DataType.STEPS, "com.google.step_count.delta",
        [("steps", "integer")], "rollup", "estimated_steps",
        from_point=_interval("steps", "count", _i),
        from_rollup=_sum_rollup("steps", "countSum", _i), additive=True),
    _ft("com.google.distance.delta", DataType.DISTANCE, "com.google.distance.delta",
        [("distance", "floatPoint")], "rollup", "merge_distance_delta",
        from_point=_interval("distance", "millimeters", _f, 1e-3),
        from_rollup=_sum_rollup("distance", "millimetersSum", _f, 1e-3), additive=True),
    _ft("com.google.calories.expended", DataType.TOTAL_CALORIES, "com.google.calories.expended",
        [("calories", "floatPoint")], "rollup", "merge_calories_expended",
        from_rollup=_sum_rollup("totalCalories", "kcalSum", _f), additive=True),
    _ft("com.google.active_minutes", DataType.ACTIVE_MINUTES, "com.google.active_minutes",
        [("duration", "integer")], "rollup", "merge_active_minutes",
        from_rollup=_active_minutes_rollup, additive=True),
    _ft("com.google.heart_minutes", DataType.ACTIVE_ZONE_MINUTES,
        "com.google.heart_minutes.summary", [("intensity", "floatPoint")], "rollup",
        "merge_heart_minutes",
        from_point=_interval("activeZoneMinutes", "activeZoneMinutes", _f),
        from_rollup=_heart_minutes_rollup, additive=True),
    _ft("com.google.heart_rate.bpm", DataType.HEART_RATE, "com.google.heart_rate.summary",
        [("bpm", "floatPoint")], "rollup", "merge_heart_rate_bpm",
        from_point=_sample("heartRate", "beatsPerMinute"), from_rollup=_heart_rate_rollup),
    _ft("com.google.hydration", DataType.HYDRATION_LOG, "com.google.hydration",
        [("volume", "floatPoint")], "rollup", "merged",
        from_point=_interval("hydrationLog", "amountConsumed.milliliters", _f, 1e-3),
        from_rollup=_hydration_rollup, additive=True),
    _ft("com.google.nutrition", DataType.NUTRITION_LOG, "com.google.nutrition.summary",
        [("nutrients", "map"), ("meal_type", "integer"), ("food_item", "string")], "rollup",
        "merged", from_point=_nutrition_point, from_rollup=_nutrition_rollup, additive=True),
    _ft("com.google.weight", DataType.WEIGHT, "com.google.weight.summary",
        [("weight", "floatPoint")], "sample", "merge_weight",
        from_point=_sample("weight", "weightGrams", 1e-3)),
    _ft("com.google.height", DataType.HEIGHT, "com.google.height.summary",
        [("height", "floatPoint")], "sample", "merge_height",
        from_point=_sample("height", "heightMillimeters", 1e-3)),
    _ft("com.google.body.fat.percentage", DataType.BODY_FAT,
        "com.google.body.fat.percentage.summary", [("percentage", "floatPoint")], "sample",
        "merged", from_point=_sample("bodyFat", "percentage")),
    _ft("com.google.oxygen_saturation", DataType.OXYGEN_SATURATION,
        "com.google.oxygen_saturation.summary", [("oxygen_saturation", "floatPoint")],
        "sample", "merged", from_point=_sample("oxygenSaturation", "percentage")),
    _ft("com.google.blood_glucose", DataType.BLOOD_GLUCOSE, "com.google.blood_glucose.summary",
        [("blood_glucose_level", "floatPoint")], "sample", "merged",
        from_point=_sample("bloodGlucose", "bloodGlucoseMilligramsPerDeciliter",
                           1 / MGDL_PER_MMOLL)),
    _ft("com.google.activity.segment", DataType.EXERCISE, "com.google.activity.summary",
        [("activity", "integer")], "activity", "merge_activity_segments",
        from_point=_exercise_point),
    _ft("com.google.sleep.segment", DataType.SLEEP, "com.google.sleep.segment",
        [("sleep_segment_type", "integer")], "sleep", "merged"),
)}

def fit_type(name_or_source_id: str) -> FitType:
    """Resolve a Fit data type name or data source id (``derived:<type>:...``)."""
    name = name_or_source_id
    if ":" in name:
        parts = name.split(":")
        name = parts[1] if len(parts) > 1 else name
    if name.endswith(".summary") and name not in FIT_TYPES:
        for t in FIT_TYPES.values():
            if t.aggregate_name == name:
                return t
    if name in FIT_TYPES:
        return FIT_TYPES[name]
    hint = " (no Google Health API equivalent)" if name in UNSUPPORTED else ""
    raise FitCompatError(f"unsupported Google Fit data type {name!r}{hint}; supported: "
                         + ", ".join(sorted(FIT_TYPES)))


# ------------------------------------------------------------------------ time
def ms_to_dt(ms: Any) -> dt.datetime:
    return dt.datetime(1970, 1, 1, tzinfo=UTC) + dt.timedelta(milliseconds=int(ms))


def ns_to_dt(ns: Any) -> dt.datetime:
    n = int(ns)
    return dt.datetime.fromtimestamp(n // 1_000_000_000, tz=UTC) + dt.timedelta(
        microseconds=(n % 1_000_000_000) // 1000)


def dt_to_ms(d: dt.datetime) -> int:
    d = d if d.tzinfo else d.replace(tzinfo=UTC)
    delta = d - dt.datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def dt_to_ns(d: dt.datetime) -> int:
    d = d if d.tzinfo else d.replace(tzinfo=UTC)
    delta = d - dt.datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _tz(name: Optional[str]) -> dt.tzinfo:
    if not name:
        return UTC
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def time_buckets(start: dt.datetime, end: dt.datetime, *,
                 duration_ms: Optional[int] = None,
                 period: Optional[Mapping[str, Any]] = None) -> List[Bucket]:
    """Fit ``bucketByTime`` boundaries. ``period`` steps in civil time of its
    ``timeZoneId`` (DST-aware); the last bucket is cut at ``end``."""
    out: List[Bucket] = []
    if duration_ms is not None:
        step = dt.timedelta(milliseconds=int(duration_ms))
        if step <= dt.timedelta(0):
            raise FitCompatError("bucketByTime.durationMillis must be positive")
        cur = start
        while cur < end:
            out.append((cur, min(cur + step, end)))
            cur += step
        return out
    if not period:
        raise FitCompatError("bucketByTime needs durationMillis or period")
    kind = str(period.get("type", "day")).lower()
    n = int(period.get("value", 1))
    if n < 1 or kind not in ("day", "week", "month"):
        raise FitCompatError(f"invalid bucketByTime.period {dict(period)!r}")
    tz = _tz(period.get("timeZoneId"))
    local0 = start.astimezone(tz).replace(tzinfo=None)
    i = 0
    cur = start
    while cur < end:
        i += 1
        if kind == "month":
            m = local0.month - 1 + n * i
            y, m = local0.year + m // 12, m % 12 + 1
            day = min(local0.day, _month_len(y, m))
            nxt_local = local0.replace(year=y, month=m, day=day)
        else:
            nxt_local = local0 + dt.timedelta(days=n * i * (7 if kind == "week" else 1))
        nxt = nxt_local.replace(tzinfo=tz).astimezone(UTC)
        out.append((cur, min(nxt, end)))
        cur = nxt
    return out


def _month_len(y: int, m: int) -> int:
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - dt.timedelta(days=1)).day


# --------------------------------------------------------------- point helpers
def _payload(ft: FitType, p: Mapping[str, Any]) -> Mapping[str, Any]:
    return p.get(ft.health.camel) or {}


def _point_times(ft: FitType, p: Mapping[str, Any]) -> Tuple[Optional[dt.datetime],
                                                             Optional[dt.datetime]]:
    body = _payload(ft, p)
    st = body.get("sampleTime")
    if isinstance(st, Mapping):
        t = parse_timestamp(st.get("physicalTime"))
        return t, t
    iv = body.get("interval") or {}
    return parse_timestamp(iv.get("startTime")), parse_timestamp(iv.get("endTime"))


def _fit_point(type_name: str, start: dt.datetime, end: dt.datetime, values: Values,
               origin: str = "", modified: Optional[dt.datetime] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "startTimeNanos": str(dt_to_ns(start)), "endTimeNanos": str(dt_to_ns(end)),
        "dataTypeName": type_name, "originDataSourceId": origin, "value": values,
    }
    if modified is not None:
        out["modifiedTimeMillis"] = str(dt_to_ms(modified))
    return out


def _origin(p: Mapping[str, Any]) -> str:
    ds = p.get("dataSource") or {}
    pkg = _get(ds, "application", "packageName")
    if pkg:
        return f"raw:{pkg}"
    dev = _get(ds, "device", "displayName")
    return f"raw:{dev}" if dev else ""


def _merge(a: Values, b: Values) -> Values:
    """Sum two additive Fit value lists element-wise."""
    out: Values = []
    for x, y in zip(a, b):
        if "intVal" in x:
            out.append(_i(x["intVal"] + y.get("intVal", 0)))
        elif "fpVal" in x:
            out.append(_f(x["fpVal"] + y.get("fpVal", 0.0)))
        elif x.get("mapVal") or y.get("mapVal"):
            acc: Dict[str, float] = {}
            for e in list(x.get("mapVal") or []) + list(y.get("mapVal") or []):
                acc[e["key"]] = acc.get(e["key"], 0.0) + float(e["value"]["fpVal"])
            out.append(_m(acc))
        else:
            out.append(x)
    return out


# ------------------------------------------------------------------- main class
class GoogleFit:
    """Google Fit REST look-alike backed by a :class:`HealthClient`.

    ``dataset_window`` sets the granularity used when a Fit dataset is requested
    for a type that only exists as a roll-up in the Google Health API
    (``calories.expended``, ``active_minutes``).
    """

    def __init__(self, client: HealthClient, *,
                 dataset_window: dt.timedelta = dt.timedelta(minutes=15)) -> None:
        self.client = client
        self.dataset_window = dataset_window

    # -- googleapiclient-style facade ------------------------------------------------
    def users(self) -> _Users:
        return _Users(self)

    # -- dataSources ----------------------------------------------------------------
    def list_data_sources(self, data_type_name: Optional[Iterable[str]] = None
                          ) -> Dict[str, Any]:
        wanted = set(data_type_name or [])
        return {"dataSource": [self._data_source(t) for t in FIT_TYPES.values()
                               if not wanted or t.name in wanted]}

    def get_data_source(self, data_source_id: str) -> Dict[str, Any]:
        return self._data_source(fit_type(data_source_id), data_source_id)

    @staticmethod
    def _data_source(ft: FitType, stream: Optional[str] = None) -> Dict[str, Any]:
        sid = stream or ft.stream
        return {
            "dataStreamId": sid, "dataStreamName": sid.rsplit(":", 1)[-1], "type": "derived",
            "dataType": {"name": ft.name,
                         "field": [{"name": n, "format": f} for n, f in ft.fields]},
            "application": {"packageName": "com.google.android.gms"},
            "dataQualityStandard": [],
        }

    # -- datasets -------------------------------------------------------------------
    def get_dataset(self, data_source_id: str, dataset_id: str, *,
                    limit: Optional[int] = None) -> Dict[str, Any]:
        """``GET users/me/dataSources/{id}/datasets/{startNanos}-{endNanos}``."""
        ft = fit_type(data_source_id)
        try:
            s_ns, e_ns = dataset_id.split("-")
            start, end = ns_to_dt(s_ns), ns_to_dt(e_ns)
        except ValueError:
            raise FitCompatError(f"dataset id must be 'startNanos-endNanos', got {dataset_id!r}"
                                 ) from None
        points = self._raw_points(ft, start, end)
        points.sort(key=lambda p: int(p["startTimeNanos"]))
        if limit is not None:
            points = points[-limit:] if limit > 0 else []
        return {"minStartTimeNs": s_ns, "maxEndTimeNs": e_ns,
                "dataSourceId": data_source_id, "point": points}

    def _raw_points(self, ft: FitType, start: dt.datetime, end: dt.datetime
                    ) -> List[Dict[str, Any]]:
        if ft.strategy == "sleep":
            return self._sleep_points(start, end)
        if ft.from_point is None:  # roll-up-only type: emit fixed windows
            out = []
            for ws, we, raw in self._windows(ft, start, end, self.dataset_window):
                vals = ft.from_rollup(raw) if ft.from_rollup else None
                if vals:
                    out.append(_fit_point(ft.name, ws, we, vals))
            return out
        out = []
        for p in self._list(ft.health, start, end):
            vals = ft.from_point(p)
            s, e = _point_times(ft, p)
            if vals is None or s is None or e is None:
                continue
            out.append(_fit_point(ft.name, s, e, vals, _origin(p)))
        return out

    def _list(self, dtype: DataType, start: dt.datetime, end: dt.datetime
              ) -> Iterable[Mapping[str, Any]]:
        """Raw points overlapping ``[start, end)`` in physical time.

        Most session types can only be filtered on civil start time (and sleep on
        end time), so the query is widened by a day and trimmed here."""
        if dtype.kind is not DataKind.SESSION:
            yield from self.client.data_points.list(dtype, start, end)
            return
        pad = dt.timedelta(days=1)
        if dtype is DataType.SLEEP:
            it = self.client.data_points.list(dtype, start, end + pad)
        else:
            it = self.client.data_points.list(
                dtype, (start - pad).replace(tzinfo=None), (end + pad).replace(tzinfo=None),
                civil=True)
        for p in it:
            iv = (p.get(dtype.camel) or {}).get("interval") or {}
            a, b = parse_timestamp(iv.get("startTime")), parse_timestamp(iv.get("endTime"))
            if a is not None and b is not None and a < end and b > start:
                yield p

    def _windows(self, ft: FitType, start: dt.datetime, end: dt.datetime,
                 window: dt.timedelta
                 ) -> Iterable[Tuple[dt.datetime, dt.datetime, Mapping[str, Any]]]:
        for raw in self.client.data_points.roll_up(ft.health, start, end, window):
            ws, we = parse_timestamp(raw.get("startTime")), parse_timestamp(raw.get("endTime"))
            if ws is not None and we is not None:
                yield ws, we, raw

    def _sleep_points(self, start: dt.datetime, end: dt.datetime) -> List[Dict[str, Any]]:
        out = []
        for p in self._list(DataType.SLEEP, start, end):
            s = p.get("sleep") or {}
            for stage in s.get("stages") or []:
                code = SLEEP_STAGE_TO_FIT.get(str(stage.get("type")))
                a, b = parse_timestamp(stage.get("startTime")), parse_timestamp(
                    stage.get("endTime"))
                if code and a and b:
                    out.append(_fit_point("com.google.sleep.segment", a, b, [_i(code)],
                                          _origin(p)))
            for seg in s.get("outOfBedSegments") or []:
                a, b = parse_timestamp(seg.get("startTime")), parse_timestamp(seg.get("endTime"))
                if a and b:
                    out.append(_fit_point("com.google.sleep.segment", a, b,
                                          [_i(FIT_OUT_OF_BED)], _origin(p)))
        return out

    # -- sessions -------------------------------------------------------------------
    def list_sessions(self, start_time: Optional[str] = None, end_time: Optional[str] = None,
                      *, activity_type: Optional[Iterable[int]] = None,
                      include_deleted: bool = False, include_sleep: bool = True
                      ) -> Dict[str, Any]:
        """``GET users/me/sessions``: exercises and sleep as Fit sessions.

        Fit defaults: without ``start_time``/``end_time`` the last 30 days."""
        end = (parse_timestamp(end_time) if end_time else None) or dt.datetime.now(UTC)
        start = (parse_timestamp(start_time) if start_time else None) or (
            end - dt.timedelta(days=30))
        wanted = set(activity_type) if activity_type is not None else None
        sessions = []
        if wanted is None or wanted - {SLEEP_ACTIVITY}:
            for p in self._list(DataType.EXERCISE, start, end):
                sess = _exercise_session(p)
                if sess and (wanted is None or sess["activityType"] in wanted):
                    sessions.append(sess)
        if include_sleep and (wanted is None or SLEEP_ACTIVITY in wanted):
            for p in self._list(DataType.SLEEP, start, end):
                sess = _sleep_session(p)
                if sess:
                    sessions.append(sess)
        sessions.sort(key=lambda x: int(x["startTimeMillis"]))
        return {"session": sessions, "deletedSession": [], "hasMoreData": False}

    # -- aggregate ------------------------------------------------------------------
    def aggregate(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        """``POST users/me/dataset:aggregate``."""
        specs = body.get("aggregateBy") or []
        if not specs:
            raise FitCompatError("aggregateBy is required")
        types = [fit_type(s.get("dataTypeName") or s.get("dataSourceId") or "") for s in specs]
        for ft in types:
            if ft.strategy == "sleep":
                raise FitCompatError("com.google.sleep.segment cannot be aggregated; "
                                     "use get_dataset() or list_sessions()")
        if "startTimeMillis" not in body or "endTimeMillis" not in body:
            raise FitCompatError("startTimeMillis and endTimeMillis are required")
        start, end = ms_to_dt(body["startTimeMillis"]), ms_to_dt(body["endTimeMillis"])
        if end <= start:
            raise FitCompatError("endTimeMillis must be after startTimeMillis")

        extra: List[Dict[str, Any]]
        merge_by: Optional[List[int]] = None
        modes = [k for k in ("bucketByTime", "bucketBySession", "bucketByActivitySegment",
                             "bucketByActivityType") if body.get(k) is not None]
        if len(modes) != 1:
            raise FitCompatError("exactly one of bucketByTime, bucketBySession, "
                                 "bucketByActivitySegment or bucketByActivityType is required")
        mode = modes[0]
        spec = body[mode] or {}
        if mode == "bucketByTime":
            buckets = time_buckets(start, end, duration_ms=spec.get("durationMillis"),
                                   period=spec.get("period"))
            extra = [{} for _ in buckets]
        else:
            min_ms = int(spec.get("minDurationMillis", 0) or 0)
            listed = self.list_sessions(_iso(start), _iso(end),
                                        include_sleep=mode == "bucketBySession")
            sess = [s for s in listed["session"]
                    if int(s["endTimeMillis"]) - int(s["startTimeMillis"]) >= min_ms]
            if mode == "bucketBySession":
                extra = [{"session": s} for s in sess]
            else:
                extra = [{"activity": s["activityType"]} for s in sess]
                if mode == "bucketByActivityType":
                    bad = [t.name for t in types if not t.additive]
                    if bad:
                        raise FitCompatError(
                            "bucketByActivityType is only supported for additive types; "
                            f"not for {', '.join(bad)}")
                    merge_by = [s["activityType"] for s in sess]
            buckets = [(max(ms_to_dt(s["startTimeMillis"]), start),
                        min(ms_to_dt(s["endTimeMillis"]), end)) for s in sess]

        per_type = [self._aggregate(ft, buckets) for ft in types]
        out: List[Dict[str, Any]] = []
        for i, (bs, be) in enumerate(buckets):
            datasets = []
            for ft, results in zip(types, per_type):
                pts = [_fit_point(name, ps, pe, vals) for name, ps, pe, vals in results[i]]
                datasets.append({"dataSourceId": _GMS.format(ft.aggregate_name, "aggregated"),
                                 "point": pts})
            out.append({"startTimeMillis": str(dt_to_ms(bs)),
                        "endTimeMillis": str(dt_to_ms(be)), **extra[i], "dataset": datasets})
        if merge_by is not None:
            out = _merge_by_activity(out, merge_by)
        return {"bucket": out}

    def _aggregate(self, ft: FitType, buckets: Sequence[Bucket]
                   ) -> List[List[Tuple[str, dt.datetime, dt.datetime, Values]]]:
        res: List[List[Tuple[str, dt.datetime, dt.datetime, Values]]] = [[] for _ in buckets]
        if not buckets:
            return res
        if ft.strategy == "rollup":
            assert ft.from_rollup is not None
            for idx, (gs, ge, win) in _uniform_groups(buckets):
                by_start = {ws: raw for ws, _, raw in self._windows(ft, gs, ge, win)}
                for i in idx:
                    raw = by_start.get(buckets[i][0])
                    vals = ft.from_rollup(raw) if raw is not None else None
                    if vals:
                        res[i].append((ft.aggregate_name, buckets[i][0], buckets[i][1], vals))
            return res
        lo, hi = min(b[0] for b in buckets), max(b[1] for b in buckets)
        if ft.strategy == "sample":
            assert ft.from_point is not None
            samples: List[List[Tuple[dt.datetime, float]]] = [[] for _ in buckets]
            for p in self._list(ft.health, lo, hi):
                t, _ = _point_times(ft, p)
                vals = ft.from_point(p)
                if t is None or not vals:
                    continue
                for i in _containing(buckets, t):
                    samples[i].append((t, float(vals[0]["fpVal"])))
            for i, ss in enumerate(samples):
                if ss:
                    xs = [v for _, v in ss]
                    res[i].append((ft.aggregate_name, min(t for t, _ in ss),
                                   max(t for t, _ in ss),
                                   [_f(sum(xs) / len(xs)), _f(max(xs)), _f(min(xs))]))
            return res
        # activity: one summary point per activity type per bucket
        exercises = []
        for p in self._list(DataType.EXERCISE, lo, hi):
            s, e = _point_times(ft, p)
            vals = _exercise_point(p)
            if s and e and vals:
                exercises.append((s, e, int(vals[0]["intVal"])))
        for i, (bs, be) in enumerate(buckets):
            acc: Dict[int, List[Any]] = {}
            for s, e, act in exercises:
                a, b = max(s, bs), min(e, be)
                if a < b:
                    cur = acc.setdefault(act, [a, b, 0, 0])
                    cur[0], cur[1] = min(cur[0], a), max(cur[1], b)
                    cur[2] += int((b - a).total_seconds() * 1000)
                    cur[3] += 1
            for act, (a, b, ms, n) in sorted(acc.items()):
                res[i].append((ft.aggregate_name, a, b, [_i(act), _i(ms), _i(n)]))
        return res


def _iso(d: dt.datetime) -> str:
    return d.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _containing(buckets: Sequence[Bucket], t: dt.datetime) -> List[int]:
    return [i for i, (s, e) in enumerate(buckets) if s <= t < e]


def _uniform_groups(buckets: Sequence[Bucket]
                    ) -> List[Tuple[List[int], Tuple[dt.datetime, dt.datetime, dt.timedelta]]]:
    """Group consecutive, contiguous buckets of equal length so each group is one
    roll-up call (window = bucket length)."""
    groups: List[Tuple[List[int], Tuple[dt.datetime, dt.datetime, dt.timedelta]]] = []
    for i, (s, e) in enumerate(buckets):
        win = e - s
        if win <= dt.timedelta(0):
            continue
        if groups:
            idx, (gs, ge, gw) = groups[-1]
            if ge == s and gw == win:
                idx.append(i)
                groups[-1] = (idx, (gs, e, gw))
                continue
        groups.append(([i], (s, e, win)))
    # A shorter final bucket (cut at endTimeMillis) gets its own call, which is
    # handled above because its length differs.
    return groups


def _merge_by_activity(buckets: List[Dict[str, Any]], acts: List[int]) -> List[Dict[str, Any]]:
    merged: Dict[int, Dict[str, Any]] = {}
    for b, act in zip(buckets, acts):
        if act not in merged:
            merged[act] = b
            continue
        m = merged[act]
        m["startTimeMillis"] = str(min(int(m["startTimeMillis"]), int(b["startTimeMillis"])))
        m["endTimeMillis"] = str(max(int(m["endTimeMillis"]), int(b["endTimeMillis"])))
        for dm, db in zip(m["dataset"], b["dataset"]):
            if not dm["point"]:
                dm["point"] = db["point"]
            elif db["point"]:
                pm, pb = dm["point"][0], db["point"][0]
                pm["value"] = _merge(pm["value"], pb["value"])
                pm["startTimeNanos"] = str(min(int(pm["startTimeNanos"]),
                                               int(pb["startTimeNanos"])))
                pm["endTimeNanos"] = str(max(int(pm["endTimeNanos"]), int(pb["endTimeNanos"])))
    return [merged[a] for a in sorted(merged)]


def _session_base(p: Mapping[str, Any], body: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    iv = body.get("interval") or {}
    s, e = parse_timestamp(iv.get("startTime")), parse_timestamp(iv.get("endTime"))
    if s is None or e is None:
        return None
    out: Dict[str, Any] = {
        "id": str(p.get("name", "")).rsplit("/", 1)[-1],
        "startTimeMillis": str(dt_to_ms(s)), "endTimeMillis": str(dt_to_ms(e)),
        "application": {"packageName": _get(p, "dataSource", "application", "packageName")
                        or "com.google.android.apps.fitness"},
    }
    upd = parse_timestamp(body.get("updateTime"))
    if upd:
        out["modifiedTimeMillis"] = str(dt_to_ms(upd))
    return out


def _exercise_session(p: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    ex = p.get("exercise") or {}
    out = _session_base(p, ex)
    if out is None:
        return None
    etype = str(ex.get("exerciseType") or "")
    out["name"] = ex.get("displayName") or etype.replace("_", " ").title()
    out["description"] = ex.get("notes") or ""
    out["activityType"] = EXERCISE_TO_FIT.get(etype, FIT_OTHER)
    active = parse_duration(ex.get("activeDuration"))
    if active is not None:
        out["activeTimeMillis"] = str(int(active.total_seconds() * 1000))
    return out


def _sleep_session(p: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    s = p.get("sleep") or {}
    out = _session_base(p, s)
    if out is None:
        return None
    nap = bool(_get(s, "metadata", "nap"))
    out["name"] = "Nap" if nap else "Sleep"
    out["description"] = ""
    out["activityType"] = SLEEP_ACTIVITY
    return out


# ------------------------------------------------- googleapiclient-style facade
class _Request:
    def __init__(self, fn: Callable[[], Dict[str, Any]]) -> None:
        self._fn = fn

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._fn()


def _check_user(user_id: str) -> None:
    if user_id != "me":
        raise FitCompatError("only userId='me' is supported")


class _Users:
    def __init__(self, fit: GoogleFit) -> None:
        self._fit = fit

    def dataset(self) -> _Dataset:
        return _Dataset(self._fit)

    def dataSources(self) -> _DataSources:  # noqa: N802 - mirrors googleapiclient
        return _DataSources(self._fit)

    def sessions(self) -> _Sessions:
        return _Sessions(self._fit)


class _Dataset:
    def __init__(self, fit: GoogleFit) -> None:
        self._fit = fit

    def aggregate(self, userId: str = "me", body: Optional[Mapping[str, Any]] = None  # noqa: N803
                  ) -> _Request:
        _check_user(userId)
        return _Request(lambda: self._fit.aggregate(body or {}))


class _DataSources:
    def __init__(self, fit: GoogleFit) -> None:
        self._fit = fit

    def list(self, userId: str = "me", dataTypeName: Any = None  # noqa: N803
             ) -> _Request:
        _check_user(userId)
        names = [dataTypeName] if isinstance(dataTypeName, str) else dataTypeName
        return _Request(lambda: self._fit.list_data_sources(names))

    def get(self, userId: str = "me", dataSourceId: str = "") -> _Request:  # noqa: N803
        _check_user(userId)
        return _Request(lambda: self._fit.get_data_source(dataSourceId))

    def datasets(self) -> _Datasets:
        return _Datasets(self._fit)


class _Datasets:
    def __init__(self, fit: GoogleFit) -> None:
        self._fit = fit

    def get(self, userId: str = "me", dataSourceId: str = "", datasetId: str = "",  # noqa: N803
            limit: Optional[int] = None, **_: Any) -> _Request:
        _check_user(userId)
        return _Request(lambda: self._fit.get_dataset(dataSourceId, datasetId, limit=limit))


class _Sessions:
    def __init__(self, fit: GoogleFit) -> None:
        self._fit = fit

    def list(self, userId: str = "me", startTime: Optional[str] = None,  # noqa: N803
             endTime: Optional[str] = None, activityType: Any = None,  # noqa: N803
             includeDeleted: bool = False, **_: Any) -> _Request:  # noqa: N803
        _check_user(userId)
        acts = [activityType] if isinstance(activityType, int) else activityType
        return _Request(lambda: self._fit.list_sessions(
            startTime, endTime, activity_type=acts, include_deleted=includeDeleted))
