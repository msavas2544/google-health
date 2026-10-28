"""Drop-in-ish replacement for the most used parts of ``python-fitbit``.

The legacy Fitbit Web API is shut down on 2 November 2026. Code written against
``python-fitbit`` can switch its import and keep most call sites::

    # before
    import fitbit
    client = fitbit.Fitbit(CLIENT_ID, CLIENT_SECRET, access_token=..., refresh_token=...)

    # after
    from google_health import fitbit_compat as fitbit
    client = fitbit.Fitbit(CLIENT_ID, CLIENT_SECRET, access_token=..., refresh_token=...)

    client.time_series("activities/steps", period="7d")
    # -> {"activities-steps": [{"dateTime": "2026-10-22", "value": "8421"}, ...]}

Responses mimic the legacy JSON shapes for the supported endpoints. They are
built from Google Health API data, so values can differ slightly from what the
legacy API returned (different aggregation and de-duplication). Note that
**OAuth tokens are not transferable**: users must re-consent with Google OAuth
(see :class:`google_health.OAuthFlow`). Use :meth:`Fitbit.legacy_user_id` to map
your stored Fitbit user ids to the new Google Health user ids.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from ._time import parse_civil_datetime, parse_duration, parse_timestamp, to_date
from .auth import OAuthCredentials
from .client import HealthClient
from .summaries import rollup_value
from .types import DataSourceFamily, DataType

US = "en_US"
METRIC = "en_GB"
UK = "en_GB"

_KM_PER_MILE = 1.609344
_KG_PER_LB = 0.45359237
_M_PER_FT = 0.3048

# legacy resource -> (data type, unit kind)
_RESOURCES: Dict[str, Tuple[DataType, str]] = {
    "activities/steps": (DataType.STEPS, "count"),
    "activities/distance": (DataType.DISTANCE, "distance"),
    "activities/floors": (DataType.FLOORS, "count"),
    "activities/elevation": (DataType.ALTITUDE, "elevation"),
    "activities/calories": (DataType.TOTAL_CALORIES, "count"),
    "activities/activityCalories": (DataType.ACTIVE_ENERGY_BURNED, "count"),
    "activities/minutesSedentary": (DataType.SEDENTARY_PERIOD, "count"),
    "body/weight": (DataType.WEIGHT, "weight"),
    "body/fat": (DataType.BODY_FAT, "float"),
    "activities/heart": (DataType.HEART_RATE, "heart"),
}

_PERIODS = {"1d": 1, "7d": 7, "30d": 30, "1w": 7, "1m": 30, "3m": 91, "6m": 182, "1y": 365}
_DETAIL = {"1sec": 1, "1min": 60, "5min": 300, "15min": 900}


class Fitbit:
    """Subset of ``fitbit.Fitbit`` backed by the Google Health API.

    ``client_id``/``client_secret`` must be your **Google** OAuth client, and the
    tokens must come from Google OAuth with Health API scopes.
    """

    US = US
    METRIC = METRIC

    def __init__(
        self,
        client_id: str,
        client_secret: Optional[str],
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        expires_at: Optional[float] = None,
        refresh_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        redirect_uri: Optional[str] = None,
        system: str = US,
        *,
        client: Optional[HealthClient] = None,
        **kwargs: Any,
    ) -> None:
        self.system = system
        # python-fitbit accepted oauth2=..., requests_kwargs=..., timeout=...; keep only
        # what HealthClient understands so old call sites don't break.
        allowed = {"user", "project", "base_url", "timeout", "max_retries", "backoff", "http"}
        kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        if client is None:
            creds = OAuthCredentials(
                access_token=access_token, refresh_token=refresh_token, client_id=client_id,
                client_secret=client_secret, expires_at=expires_at,
                on_refresh=(lambda c: refresh_cb(c.to_dict())) if refresh_cb else None,
            )
            client = HealthClient(creds, **kwargs)
        self.client = client
        self._settings: Optional[Dict[str, Any]] = None

    # -- helpers -----------------------------------------------------------------------
    @property
    def _metric(self) -> bool:
        return self.system != US

    def _convert(self, kind: str, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if kind == "distance":  # metres -> km / miles
            km = value / 1000.0
            return km if self._metric else km / _KM_PER_MILE
        if kind == "elevation":  # metres -> m / ft
            return value if self._metric else value / _M_PER_FT
        if kind == "weight":  # kg -> kg / lb
            return value if self._metric else value / _KG_PER_LB
        return value

    @staticmethod
    def _fmt(kind: str, value: Optional[float]) -> str:
        if value is None:
            return "0"
        if kind == "count":
            return str(int(round(value)))
        return f"{value:.2f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))

    def _today(self) -> dt.date:
        """'today' in the *user's* time zone (the legacy API's semantics)."""
        try:
            tz = self._user_tz()
        except Exception:
            tz = None
        return dt.datetime.now(tz).date() if tz else dt.date.today()

    def _day(self, value: Optional[Union[str, dt.date]]) -> dt.date:
        if value is None or value == "today":
            return self._today()
        return to_date(value)

    def _date_range(self, base_date: Union[str, dt.date], period: Optional[str],
                    end_date: Optional[Union[str, dt.date]]) -> Tuple[dt.date, dt.date]:
        """Return an inclusive ``(first, last)`` range using legacy semantics."""
        if period and end_date:
            raise TypeError("Either end_date or period can be specified, not both")
        if end_date is not None:
            return self._day(base_date), self._day(end_date)
        if period:
            if period not in _PERIODS:
                raise ValueError(f"Period must be one of {sorted(_PERIODS)}")
            last = self._day(base_date)
            return last - dt.timedelta(days=_PERIODS[period] - 1), last
        d = self._day(base_date)
        return d, d

    def _user_tz(self) -> Union[dt.tzinfo, None]:
        if self._settings is None:
            self._settings = self.client.users.get_settings()
        tz = self._settings.get("timeZone")
        tz_id = tz.get("id") if isinstance(tz, dict) else tz
        if tz_id:
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(tz_id)
            except Exception:
                pass
        off = parse_duration(self._settings.get("utcOffset"))
        return dt.timezone(off) if off is not None else None

    @staticmethod
    def _resource(resource: str) -> Tuple[str, DataType, str, Optional[str]]:
        family = None
        r = resource.strip("/")
        if "/tracker/" in r:
            r = r.replace("/tracker/", "/")
            family = DataSourceFamily.GOOGLE_WEARABLES.value
        if r not in _RESOURCES:
            raise ValueError(
                f"unsupported resource {resource!r}; supported: {', '.join(sorted(_RESOURCES))}")
        dtype, kind = _RESOURCES[r]
        return r, dtype, kind, family

    # -- user / devices ----------------------------------------------------------------
    def legacy_user_id(self) -> Optional[str]:
        """The user's old Fitbit encoded id (to re-key rows in your database)."""
        return self.client.users.get_identity().get("legacyUserId")

    def user_profile_get(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        ident = self.client.users.get_identity()
        prof = self.client.users.get_profile()
        settings = self.client.users.get_settings()
        self._settings = settings
        off = parse_duration(settings.get("utcOffset"))
        tz = settings.get("timeZone")
        return {"user": {
            "encodedId": ident.get("legacyUserId"),
            "healthUserId": ident.get("healthUserId"),
            "age": prof.get("age"),
            "memberSince": _date_str(prof.get("membershipStartDate")),
            "timezone": tz.get("id") if isinstance(tz, dict) else tz,
            "offsetFromUTCMillis": int(off.total_seconds() * 1000) if off else 0,
            "distanceUnit": settings.get("distanceUnit"),
            "weightUnit": settings.get("weightUnit"),
            "heightUnit": settings.get("heightUnit"),
            "locale": settings.get("languageLocale"),
            "strideLengthWalking": _mm_to_cm(prof.get("userConfiguredWalkingStrideLengthMm")
                                             or prof.get("autoWalkingStrideLengthMm")),
            "strideLengthRunning": _mm_to_cm(prof.get("userConfiguredRunningStrideLengthMm")
                                             or prof.get("autoRunningStrideLengthMm")),
        }}

    def get_devices(self) -> List[Dict[str, Any]]:
        out = []
        for d in self.client.devices.list(page_size=100):
            out.append({
                "id": str(d.get("name", "")).rsplit("/", 1)[-1],
                "battery": (d.get("batteryStatus") or "").title() or None,
                "batteryLevel": d.get("batteryLevel"),
                "deviceVersion": d.get("deviceVersion"),
                "lastSyncTime": d.get("lastSyncTime"),
                "type": d.get("deviceType"),
                "mac": d.get("macAddress"),
                "features": d.get("features", []),
            })
        return out

    # -- time series -------------------------------------------------------------------
    def time_series(
        self,
        resource: str,
        user_id: Optional[str] = None,
        base_date: Union[str, dt.date] = "today",
        period: Optional[str] = None,
        end_date: Optional[Union[str, dt.date]] = None,
    ) -> Dict[str, Any]:
        r, dtype, kind, family = self._resource(resource)
        first, last = self._date_range(base_date, period, end_date)
        key = resource.strip("/").replace("/", "-")
        if kind == "heart":
            return {key: self._resting_heart(first, last)}
        days = {d.date: d.value for d in self.client.daily(
            dtype, first, last + dt.timedelta(days=1), data_source_family=family)}
        series = []
        cur = first
        while cur <= last:
            series.append({"dateTime": cur.isoformat(),
                           "value": self._fmt(kind, self._convert(kind, days.get(cur)))})
            cur += dt.timedelta(days=1)
        return {key: series}

    def _resting_heart(self, first: dt.date, last: dt.date) -> List[Dict[str, Any]]:
        by_day: Dict[dt.date, int] = {}
        for p in self.client.data_points.list(
                DataType.DAILY_RESTING_HEART_RATE, first, last + dt.timedelta(days=1)):
            v = p.get("dailyRestingHeartRate") or {}
            d = _parse_date(v.get("date"))
            if d and v.get("beatsPerMinute") is not None:
                by_day[d] = int(v["beatsPerMinute"])
        out = []
        cur = first
        while cur <= last:
            value: Dict[str, Any] = {"customHeartRateZones": [], "heartRateZones": []}
            if cur in by_day:
                value["restingHeartRate"] = by_day[cur]
            out.append({"dateTime": cur.isoformat(), "value": value})
            cur += dt.timedelta(days=1)
        return out

    def intraday_time_series(
        self,
        resource: str,
        base_date: Union[str, dt.date] = "today",
        detail_level: str = "1min",
        start_time: Optional[Union[str, dt.time]] = None,
        end_time: Optional[Union[str, dt.time]] = None,
    ) -> Dict[str, Any]:
        if detail_level not in _DETAIL:
            raise ValueError(f"Period must be one of {sorted(_DETAIL)}")
        if (start_time is None) != (end_time is None):
            raise TypeError("You must provide both the end and start time or neither")
        r, dtype, kind, family = self._resource(resource)
        day = self._day(base_date)
        tz = self._user_tz() or dt.timezone.utc
        st = _parse_time(start_time) if start_time is not None else dt.time(0, 0)
        local_start = dt.datetime.combine(day, st).replace(tzinfo=tz)
        if end_time is not None:
            local_end = dt.datetime.combine(day, _parse_time(end_time)).replace(tzinfo=tz)
            # Legacy end_time is inclusive: extend to the end of the bucket containing it.
            step_td = dt.timedelta(seconds=_DETAIL[detail_level])
            if local_end < local_start:
                raise ValueError("end_time must not be before start_time")
            local_end = local_start + step_td * ((local_end - local_start) // step_td + 1)
        else:
            local_end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0)).replace(
                tzinfo=tz)
        step = _DETAIL[detail_level]
        dataset = []
        total = 0.0
        for w in self.client.windows(dtype, local_start, local_end, step,
                                     data_source_family=family):
            raw_val = rollup_value(dtype, w.raw) if kind != "heart" else (
                (w.raw.get("heartRate") or {}).get("beatsPerMinuteAvg"))
            if raw_val is None:
                continue
            val = self._convert(kind, raw_val)
            assert val is not None
            total += val
            local = w.start.astimezone(tz)
            dataset.append({"time": local.strftime("%H:%M:%S"),
                            "value": int(round(val)) if kind in ("count", "heart") else val})
        key = r.replace("/", "-")
        summary_value: Any = self._fmt(kind, total) if kind != "heart" else None
        return {
            key: [{"dateTime": day.isoformat(), "value": summary_value}],
            f"{key}-intraday": {
                "dataset": dataset,
                "datasetInterval": 1 if detail_level == "1sec" else step // 60,
                "datasetType": "second" if detail_level == "1sec" else "minute",
            },
        }

    # -- sleep / body ------------------------------------------------------------------
    def get_sleep(self, date: Union[str, dt.date]) -> Dict[str, Any]:
        """Sleep sessions that *ended* on ``date`` (legacy "dateOfSleep" semantics)."""
        day = self._day(date)
        sessions = []
        for p in self.client.data_points.list(DataType.SLEEP, day, day + dt.timedelta(days=1),
                                              civil=True):
            sessions.append(_legacy_sleep(p, day))
        sessions.sort(key=lambda s: s["startTime"] or "")
        return {"sleep": sessions, "summary": {
            "totalMinutesAsleep": sum(s["minutesAsleep"] for s in sessions),
            "totalSleepRecords": len(sessions),
            "totalTimeInBed": sum(s["timeInBed"] for s in sessions),
        }}

    def get_bodyweight(
        self,
        base_date: Optional[Union[str, dt.date]] = None,
        user_id: Optional[str] = None,
        period: Optional[str] = None,
        end_date: Optional[Union[str, dt.date]] = None,
    ) -> Dict[str, Any]:
        first, last = self._date_range(base_date or "today", period, end_date)
        out = []
        for p in self.client.data_points.list(DataType.WEIGHT, first,
                                              last + dt.timedelta(days=1), civil=True):
            w = p.get("weight") or {}
            when = parse_civil_datetime((w.get("sampleTime") or {}).get("civilTime"))
            grams = w.get("weightGrams")
            if grams is None:
                continue
            out.append({
                "date": when.date().isoformat() if when else None,
                "time": when.strftime("%H:%M:%S") if when else None,
                "weight": round(self._convert("weight", float(grams) / 1000.0) or 0, 2),
                "logId": str(p.get("name", "")).rsplit("/", 1)[-1] or None,
                "source": _source(p),
            })
        out.sort(key=lambda x: (x["date"] or "", x["time"] or ""))
        return {"weight": out}

    def get_bodyfat(
        self,
        base_date: Optional[Union[str, dt.date]] = None,
        user_id: Optional[str] = None,
        period: Optional[str] = None,
        end_date: Optional[Union[str, dt.date]] = None,
    ) -> Dict[str, Any]:
        first, last = self._date_range(base_date or "today", period, end_date)
        out = []
        for p in self.client.data_points.list(DataType.BODY_FAT, first,
                                              last + dt.timedelta(days=1), civil=True):
            f = p.get("bodyFat") or {}
            when = parse_civil_datetime((f.get("sampleTime") or {}).get("civilTime"))
            out.append({
                "date": when.date().isoformat() if when else None,
                "time": when.strftime("%H:%M:%S") if when else None,
                "fat": f.get("percentage"),
                "logId": str(p.get("name", "")).rsplit("/", 1)[-1] or None,
                "source": _source(p),
            })
        out.sort(key=lambda x: (x["date"] or "", x["time"] or ""))
        return {"fat": out}

    def activities(self, date: Optional[Union[str, dt.date]] = None,
                   user_id: Optional[str] = None) -> Dict[str, Any]:
        """Daily activity summary (``GET /activities/date/{date}.json`` subset)."""
        day = self._day(date)
        nxt = day + dt.timedelta(days=1)

        def one(dtype: DataType) -> Optional[float]:
            vals = self.client.daily(dtype, day, nxt)
            return vals[0].value if vals else None

        dist = self._convert("distance", one(DataType.DISTANCE))
        summary = {
            "steps": int(one(DataType.STEPS) or 0),
            "caloriesOut": int(one(DataType.TOTAL_CALORIES) or 0),
            "activityCalories": int(one(DataType.ACTIVE_ENERGY_BURNED) or 0),
            "floors": int(one(DataType.FLOORS) or 0),
            "elevation": round(self._convert("elevation", one(DataType.ALTITUDE)) or 0, 2),
            "sedentaryMinutes": int(one(DataType.SEDENTARY_PERIOD) or 0),
            "distances": [{"activity": "total", "distance": round(dist or 0, 2)}],
        }
        return {"activities": [], "goals": {}, "summary": summary}


# ---------------------------------------------------------------------------- utilities
def _parse_time(t: Union[str, dt.time]) -> dt.time:
    if isinstance(t, dt.time):
        return t
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", t)
    if not m:
        raise ValueError(f"invalid time {t!r}; expected HH:MM")
    return dt.time(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _parse_date(msg: Any) -> Optional[dt.date]:
    if isinstance(msg, dict) and msg.get("year"):
        return dt.date(int(msg["year"]), int(msg["month"]), int(msg["day"]))
    if isinstance(msg, str):
        return to_date(msg)
    return None


def _date_str(msg: Any) -> Optional[str]:
    d = _parse_date(msg)
    return d.isoformat() if d else None


def _mm_to_cm(v: Any) -> Optional[float]:
    return round(float(v) / 10.0, 1) if v is not None else None


def _source(p: Dict[str, Any]) -> str:
    ds = p.get("dataSource") or {}
    if ds.get("device"):
        return "Aria" if "scale" in str(ds["device"]).lower() else "Device"
    if ds.get("application"):
        return "API"
    return "Unknown"


_STAGE_KEYS = {"DEEP": "deep", "LIGHT": "light", "REM": "rem", "AWAKE": "wake",
               "ASLEEP": "asleep", "RESTLESS": "restless"}
# Legacy "classic" sleep logs used asleep / restless / awake.
_CLASSIC_KEYS = {"AWAKE": "awake", "ASLEEP": "asleep", "RESTLESS": "restless"}


def _legacy_sleep(p: Dict[str, Any], day: dt.date) -> Dict[str, Any]:
    s = p.get("sleep") or {}
    iv = s.get("interval") or {}
    summ = s.get("summary") or {}
    meta = s.get("metadata") or {}
    start = parse_civil_datetime(iv.get("civilStartTime"))
    end = parse_civil_datetime(iv.get("civilEndTime"))
    ps, pe = parse_timestamp(iv.get("startTime")), parse_timestamp(iv.get("endTime"))
    duration_ms = int((pe - ps).total_seconds() * 1000) if ps and pe else 0
    stages = {}
    keys = _STAGE_KEYS if s.get("type") == "STAGES" else _CLASSIC_KEYS
    for st in summ.get("stagesSummary") or []:
        k = keys.get(str(st.get("type")))
        if k:
            stages[k] = {"count": int(st.get("count", 0) or 0),
                         "minutes": int(st.get("minutes", 0) or 0)}

    def i(key: str) -> int:
        return int(summ.get(key, 0) or 0)

    return {
        "dateOfSleep": day.isoformat(),
        "logId": str(p.get("name", "")).rsplit("/", 1)[-1] or None,
        "startTime": start.isoformat(timespec="milliseconds") if start else None,
        "endTime": end.isoformat(timespec="milliseconds") if end else None,
        "duration": duration_ms,
        "minutesAsleep": i("minutesAsleep"),
        "minutesAwake": i("minutesAwake"),
        "minutesAfterWakeup": i("minutesAfterWakeUp"),
        "minutesToFallAsleep": i("minutesToFallAsleep"),
        "timeInBed": i("minutesInSleepPeriod"),
        "efficiency": (round(100 * i("minutesAsleep") / i("minutesInSleepPeriod"))
                       if i("minutesInSleepPeriod") else None),
        "isMainSleep": bool(meta.get("mainSleep")),
        "type": "stages" if s.get("type") == "STAGES" else "classic",
        "levels": {"summary": stages},
    }
