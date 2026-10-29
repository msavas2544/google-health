from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from google_health.googlefit_compat import (
    FitCompatError,
    GoogleFit,
    dt_to_ms,
    dt_to_ns,
    fit_type,
    ms_to_dt,
    ns_to_dt,
    time_buckets,
)

UTC = dt.timezone.utc
DAY = 86_400_000
T0 = dt.datetime(2026, 10, 20, tzinfo=UTC)
T0_MS = dt_to_ms(T0)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rollup(api, data_type: str, values: dict) -> list:
    """Serve ``dataPoints:rollUp`` windows; ``values`` maps window-start ISO -> payload."""
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        b = json.loads(request.content)
        calls.append(b)
        s = dt.datetime.fromisoformat(b["range"]["startTime"].replace("Z", "+00:00"))
        e = dt.datetime.fromisoformat(b["range"]["endTime"].replace("Z", "+00:00"))
        win = dt.timedelta(seconds=float(b["windowSize"][:-1]))
        pts = []
        cur = s
        while cur < e:
            nxt = min(cur + win, e)
            if _iso(cur) in values:
                pts.append({"startTime": _iso(cur), "endTime": _iso(nxt), **values[_iso(cur)]})
            cur = nxt
        return httpx.Response(200, json={"rollupDataPoints": pts})

    api.add("POST", f"/{data_type}/dataPoints:rollUp", handler)
    return calls


def _points(api, data_type: str, points: list) -> None:
    api.add("GET", f"/{data_type}/dataPoints", {"dataPoints": points})


def _body(types, start_ms=T0_MS, end_ms=T0_MS + 3 * DAY, **bucket):
    return {"aggregateBy": [{"dataTypeName": t} for t in types],
            "startTimeMillis": str(start_ms), "endTimeMillis": end_ms, **bucket}


# ------------------------------------------------------------------- time helpers
def test_time_conversions_roundtrip() -> None:
    d = dt.datetime(2026, 10, 20, 7, 30, 15, 123456, tzinfo=UTC)
    assert ns_to_dt(dt_to_ns(d)) == d
    assert ms_to_dt(dt_to_ms(d)) == d.replace(microsecond=123000)
    assert dt_to_ns(T0) == T0_MS * 1_000_000


def test_period_buckets_follow_local_days_across_dst() -> None:
    tz = "America/New_York"
    start = dt.datetime(2026, 10, 31, 4, tzinfo=UTC)  # 00:00 EDT
    end = dt.datetime(2026, 11, 3, 5, tzinfo=UTC)  # 00:00 EST
    b = time_buckets(start, end, period={"type": "day", "value": 1, "timeZoneId": tz})
    assert [(e - s).total_seconds() / 3600 for s, e in b] == [24, 25, 24]


def test_month_and_week_buckets() -> None:
    start = dt.datetime(2026, 1, 31, tzinfo=UTC)
    end = dt.datetime(2026, 4, 1, tzinfo=UTC)
    b = time_buckets(start, end, period={"type": "month", "value": 1})
    assert [s.date().isoformat() for s, _ in b] == ["2026-01-31", "2026-02-28", "2026-03-31"]
    w = time_buckets(T0, T0 + dt.timedelta(days=10), period={"type": "week", "value": 1})
    assert len(w) == 2 and w[1][1] == T0 + dt.timedelta(days=10)
    with pytest.raises(FitCompatError):
        time_buckets(T0, T0 + dt.timedelta(days=1), period={"type": "year"})


def test_fit_type_resolution() -> None:
    assert fit_type("derived:com.google.step_count.delta:com.google.android.gms:"
                    "estimated_steps").health.value == "steps"
    assert fit_type("com.google.weight.summary").name == "com.google.weight"
    with pytest.raises(FitCompatError, match="no Google Health API equivalent"):
        fit_type("com.google.blood_pressure")


# --------------------------------------------------------------------- aggregate
def test_aggregate_steps_daily_duration(api, client) -> None:
    calls = _rollup(api, "steps", {
        _iso(T0): {"steps": {"countSum": "8421"}},
        _iso(T0 + dt.timedelta(days=2)): {"steps": {"countSum": "1200"}},
    })
    out = GoogleFit(client).aggregate(_body(["com.google.step_count.delta"],
                                            bucketByTime={"durationMillis": DAY}))
    b = out["bucket"]
    assert len(b) == 3 and len(calls) == 1 and calls[0]["windowSize"] == "86400s"
    assert b[0]["startTimeMillis"] == str(T0_MS) and b[0]["endTimeMillis"] == str(T0_MS + DAY)
    ds = b[0]["dataset"][0]
    assert ds["dataSourceId"].startswith("derived:com.google.step_count.delta:")
    p = ds["point"][0]
    assert p["value"][0]["intVal"] == 8421 and p["dataTypeName"] == "com.google.step_count.delta"
    assert p["startTimeNanos"] == str(T0_MS * 1_000_000)
    assert b[1]["dataset"][0]["point"] == []
    assert b[2]["dataset"][0]["point"][0]["value"][0]["intVal"] == 1200


def test_aggregate_period_in_time_zone_and_partial_last_bucket(api, client) -> None:
    start = dt.datetime(2026, 10, 19, 21, tzinfo=UTC)  # 00:00 in Istanbul (UTC+3)
    calls = _rollup(api, "distance", {_iso(start): {"distance": {"millimetersSum": "5250000"}}})
    out = GoogleFit(client).aggregate(_body(
        ["com.google.distance.delta"], dt_to_ms(start), dt_to_ms(start) + DAY + DAY // 2,
        bucketByTime={"period": {"type": "day", "value": 1, "timeZoneId": "Europe/Istanbul"}}))
    assert len(out["bucket"]) == 2
    assert out["bucket"][0]["dataset"][0]["point"][0]["value"][0]["fpVal"] == 5250.0
    # full day + shorter tail -> two roll-up calls with different windows
    assert [c["windowSize"] for c in calls] == ["86400s", "43200s"]


def test_aggregate_multiple_types_heart_rate_summary(api, client) -> None:
    _rollup(api, "total-calories", {_iso(T0): {"totalCalories": {"kcalSum": 2100.5}}})
    _rollup(api, "heart-rate", {_iso(T0): {"heartRate": {
        "beatsPerMinuteAvg": 71.5, "beatsPerMinuteMax": 150, "beatsPerMinuteMin": 48}}})
    out = GoogleFit(client).aggregate(_body(
        ["com.google.calories.expended", "com.google.heart_rate.bpm"], T0_MS, T0_MS + DAY,
        bucketByTime={"durationMillis": DAY}))
    cal, hr = out["bucket"][0]["dataset"]
    assert cal["point"][0]["value"][0]["fpVal"] == 2100.5
    assert hr["dataSourceId"].startswith("derived:com.google.heart_rate.summary:")
    assert [v["fpVal"] for v in hr["point"][0]["value"]] == [71.5, 150.0, 48.0]


def test_aggregate_active_heart_minutes_hydration_nutrition(api, client) -> None:
    _rollup(api, "active-minutes", {_iso(T0): {"activeMinutes": {
        "activeMinutesRollupByActivityLevel": [
            {"activityLevel": "MODERATE", "activeMinutesSum": "20"},
            {"activityLevel": "VIGOROUS", "activeMinutesSum": "10"}]}}})
    _rollup(api, "active-zone-minutes", {_iso(T0): {"activeZoneMinutes": {
        "sumInFatBurnHeartZone": "10", "sumInCardioHeartZone": "8",
        "sumInPeakHeartZone": "4"}}})
    _rollup(api, "hydration-log", {_iso(T0): {"hydrationLog": {
        "amountConsumed": {"millilitersSum": 1750}}}})
    _rollup(api, "nutrition-log", {_iso(T0): {"nutritionLog": {
        "energy": {"kcalSum": 650}, "totalFat": {"gramsSum": 20},
        "nutrients": [{"nutrient": "PROTEIN", "quantity": {"gramsSum": 30}},
                      {"nutrient": "SODIUM", "quantity": {"gramsSum": 0.5}},
                      {"nutrient": "BIOTIN", "quantity": {"gramsSum": 0.001}}]}}})
    out = GoogleFit(client).aggregate(_body(
        ["com.google.active_minutes", "com.google.heart_minutes", "com.google.hydration",
         "com.google.nutrition"], T0_MS, T0_MS + DAY, bucketByTime={"durationMillis": DAY}))
    am, hm, hy, nu = (d["point"][0]["value"] for d in out["bucket"][0]["dataset"])
    assert am[0]["intVal"] == 30
    assert hm[0]["fpVal"] == 22.0 and hm[1]["intVal"] == 16
    assert hy[0]["fpVal"] == 1.75
    nutrients = {e["key"]: e["value"]["fpVal"] for e in nu[0]["mapVal"]}
    assert nutrients == {"calories": 650.0, "fat.total": 20.0, "protein": 30.0, "sodium": 500.0}


def test_aggregate_weight_samples_avg_max_min(api, client) -> None:
    def w(t: dt.datetime, grams: float) -> dict:
        return {"weight": {"weightGrams": grams, "sampleTime": {"physicalTime": _iso(t)}}}

    _points(api, "weight", [w(T0 + dt.timedelta(hours=7), 72000),
                            w(T0 + dt.timedelta(hours=20), 73000),
                            w(T0 + dt.timedelta(days=1, hours=8), 71500)])
    out = GoogleFit(client).aggregate(_body(["com.google.weight"], T0_MS, T0_MS + 2 * DAY,
                                            bucketByTime={"durationMillis": DAY}))
    day1 = out["bucket"][0]["dataset"][0]["point"][0]
    assert day1["dataTypeName"] == "com.google.weight.summary"
    assert [v["fpVal"] for v in day1["value"]] == [72.5, 73.0, 72.0]
    assert out["bucket"][1]["dataset"][0]["point"][0]["value"][0]["fpVal"] == 71.5


def test_blood_glucose_in_mmol(api, client) -> None:
    _points(api, "blood-glucose", [{"bloodGlucose": {
        "bloodGlucoseMilligramsPerDeciliter": 90.08,
        "sampleTime": {"physicalTime": _iso(T0 + dt.timedelta(hours=1))}}}])
    out = GoogleFit(client).aggregate(_body(["com.google.blood_glucose"], T0_MS, T0_MS + DAY,
                                            bucketByTime={"durationMillis": DAY}))
    assert out["bucket"][0]["dataset"][0]["point"][0]["value"][0]["fpVal"] == pytest.approx(5.0)


def _exercise(pid: str, etype: str, start: dt.datetime, minutes: int) -> dict:
    return {"name": f"users/me/dataTypes/exercise/dataPoints/{pid}",
            "dataSource": {"application": {"packageName": "com.example.run"}},
            "exercise": {"exerciseType": etype, "displayName": f"{etype} {pid}",
                         "activeDuration": f"{minutes * 60 - 30}s",
                         "updateTime": _iso(start + dt.timedelta(minutes=minutes)),
                         "interval": {"startTime": _iso(start),
                                      "endTime": _iso(start + dt.timedelta(minutes=minutes))}}}


EXERCISES = [_exercise("e1", "RUNNING", T0 + dt.timedelta(hours=7), 30),
             _exercise("e2", "WALKING", T0 + dt.timedelta(hours=12), 20),
             _exercise("e3", "RUNNING", T0 + dt.timedelta(hours=18), 40)]


def test_aggregate_activity_summary(api, client) -> None:
    _points(api, "exercise", EXERCISES)
    out = GoogleFit(client).aggregate(_body(["com.google.activity.segment"], T0_MS, T0_MS + DAY,
                                            bucketByTime={"durationMillis": DAY}))
    pts = out["bucket"][0]["dataset"][0]["point"]
    assert [[v["intVal"] for v in p["value"]] for p in pts] == [[7, 20 * 60000, 1],
                                                                 [8, 70 * 60000, 2]]


def test_bucket_by_activity_type_merges_sessions(api, client) -> None:
    _points(api, "exercise", EXERCISES)
    _rollup(api, "steps", {
        _iso(T0 + dt.timedelta(hours=7)): {"steps": {"countSum": "4000"}},
        _iso(T0 + dt.timedelta(hours=12)): {"steps": {"countSum": "2000"}},
        _iso(T0 + dt.timedelta(hours=18)): {"steps": {"countSum": "5000"}},
    })
    out = GoogleFit(client).aggregate(_body(["com.google.step_count.delta"], T0_MS, T0_MS + DAY,
                                            bucketByActivityType={}))
    got = {b["activity"]: b["dataset"][0]["point"][0]["value"][0]["intVal"]
           for b in out["bucket"]}
    assert got == {8: 9000, 7: 2000}
    with pytest.raises(FitCompatError, match="additive"):
        GoogleFit(client).aggregate(_body(["com.google.heart_rate.bpm"], T0_MS, T0_MS + DAY,
                                          bucketByActivityType={}))


def test_bucket_by_session_includes_sleep_and_min_duration(api, client) -> None:
    _points(api, "exercise", EXERCISES)
    _points(api, "sleep", [{"name": "users/me/dataTypes/sleep/dataPoints/s1", "sleep": {
        "metadata": {"nap": False},
        "interval": {"startTime": _iso(T0 - dt.timedelta(hours=1)),
                     "endTime": _iso(T0 + dt.timedelta(hours=6))}}}])
    _rollup(api, "steps", {})
    out = GoogleFit(client).aggregate(_body(["com.google.step_count.delta"], T0_MS, T0_MS + DAY,
                                            bucketBySession={"minDurationMillis": 25 * 60000}))
    names = [b["session"]["name"] for b in out["bucket"]]
    assert names == ["Sleep", "RUNNING e1", "RUNNING e3"]
    # the sleep session is clipped to the requested range
    assert out["bucket"][0]["startTimeMillis"] == str(T0_MS)


def test_aggregate_errors(client) -> None:
    fit = GoogleFit(client)
    with pytest.raises(FitCompatError):
        fit.aggregate({"aggregateBy": []})
    with pytest.raises(FitCompatError, match="cannot be aggregated"):
        fit.aggregate(_body(["com.google.sleep.segment"], bucketByTime={"durationMillis": DAY}))
    with pytest.raises(FitCompatError, match="exactly one"):
        fit.aggregate(_body(["com.google.step_count.delta"]))
    with pytest.raises(FitCompatError, match="after"):
        fit.aggregate(_body(["com.google.step_count.delta"], T0_MS, T0_MS,
                            bucketByTime={"durationMillis": DAY}))


# ----------------------------------------------------------------------- datasets
def test_get_dataset_steps_raw_points(api, client) -> None:
    def s(minute: int, count: str) -> dict:
        a = T0 + dt.timedelta(minutes=minute)
        return {"dataSource": {"application": {"packageName": "com.fitbit.FitbitMobile"}},
                "steps": {"count": count, "interval": {
                    "startTime": _iso(a), "endTime": _iso(a + dt.timedelta(minutes=1))}}}

    _points(api, "steps", [s(10, "80"), s(5, "120")])  # API returns newest first
    ds_id = "derived:com.google.step_count.delta:com.google.android.gms:estimated_steps"
    out = GoogleFit(client).get_dataset(ds_id, f"{dt_to_ns(T0)}-{dt_to_ns(T0) + 3600 * 10**9}")
    assert out["dataSourceId"] == ds_id and out["minStartTimeNs"] == str(dt_to_ns(T0))
    assert [p["value"][0]["intVal"] for p in out["point"]] == [120, 80]
    assert out["point"][0]["originDataSourceId"] == "raw:com.fitbit.FitbitMobile"
    assert "physical_time" in api.requests[0].url.params["filter"] or \
        "interval" in api.requests[0].url.params["filter"]
    with pytest.raises(FitCompatError):
        GoogleFit(client).get_dataset(ds_id, "bogus")


def test_get_dataset_rollup_only_type_uses_windows(api, client) -> None:
    calls = _rollup(api, "total-calories", {
        _iso(T0): {"totalCalories": {"kcalSum": 25}},
        _iso(T0 + dt.timedelta(minutes=30)): {"totalCalories": {"kcalSum": 40}}})
    out = GoogleFit(client).get_dataset("com.google.calories.expended",
                                        f"{dt_to_ns(T0)}-{dt_to_ns(T0) + 3600 * 10**9}")
    assert calls[0]["windowSize"] == "900s"
    assert [p["value"][0]["fpVal"] for p in out["point"]] == [25.0, 40.0]


def test_get_dataset_sleep_segments(api, client) -> None:
    def st(kind: str, a: int, b: int) -> dict:
        return {"type": kind, "startTime": _iso(T0 + dt.timedelta(minutes=a)),
                "endTime": _iso(T0 + dt.timedelta(minutes=b))}

    _points(api, "sleep", [{"sleep": {
        "interval": {"startTime": _iso(T0), "endTime": _iso(T0 + dt.timedelta(minutes=130))},
        "stages": [st("LIGHT", 0, 30), st("DEEP", 30, 90), st("REM", 90, 120),
                   st("AWAKE", 120, 125), st("SLEEP_TYPE_UNKNOWN", 125, 126)],
        "outOfBedSegments": [st("", 126, 130)]}}])
    out = GoogleFit(client).get_dataset("com.google.sleep.segment",
                                        f"{dt_to_ns(T0)}-{dt_to_ns(T0) + 6 * 3600 * 10**9}")
    assert [p["value"][0]["intVal"] for p in out["point"]] == [4, 5, 6, 1, 3]


# ----------------------------------------------------------------------- sessions
def test_list_sessions_and_activity_filter(api, client) -> None:
    _points(api, "exercise", EXERCISES)
    _points(api, "sleep", [{"sleep": {"metadata": {"nap": True}, "interval": {
        "startTime": _iso(T0 + dt.timedelta(hours=14)),
        "endTime": _iso(T0 + dt.timedelta(hours=15))}}}])
    fit = GoogleFit(client)
    out = fit.list_sessions(_iso(T0), _iso(T0 + dt.timedelta(days=1)))
    assert [s["activityType"] for s in out["session"]] == [8, 7, 72, 8]
    first = out["session"][0]
    assert first["id"] == "e1" and first["activeTimeMillis"] == str(1770 * 1000)
    assert first["application"]["packageName"] == "com.example.run"
    assert "modifiedTimeMillis" in first and out["deletedSession"] == []
    n = len(api.requests)
    only_sleep = fit.list_sessions(_iso(T0), _iso(T0 + dt.timedelta(days=1)), activity_type=[72])
    assert [s["name"] for s in only_sleep["session"]] == ["Nap"]
    assert len(api.requests) == n + 1  # exercise list skipped


# --------------------------------------------------------- googleapiclient facade
def test_facade_mirrors_googleapiclient(api, client) -> None:
    _rollup(api, "steps", {_iso(T0): {"steps": {"countSum": "10"}}})
    _points(api, "exercise", EXERCISES)
    _points(api, "sleep", [])
    fit = GoogleFit(client)
    users = fit.users()
    agg = users.dataset().aggregate(userId="me", body=_body(
        ["com.google.step_count.delta"], T0_MS, T0_MS + DAY,
        bucketByTime={"durationMillis": DAY})).execute()
    assert agg["bucket"][0]["dataset"][0]["point"][0]["value"][0]["intVal"] == 10
    sources = users.dataSources().list(userId="me",
                                       dataTypeName="com.google.weight").execute()
    assert [s["dataType"]["name"] for s in sources["dataSource"]] == ["com.google.weight"]
    assert len(users.dataSources().list(userId="me").execute()["dataSource"]) >= 15
    src = users.dataSources().get(userId="me", dataSourceId="com.google.height").execute()
    assert src["dataType"]["field"] == [{"name": "height", "format": "floatPoint"}]
    sess = users.sessions().list(userId="me", startTime=_iso(T0),
                                 endTime=_iso(T0 + dt.timedelta(days=1)),
                                 activityType=8).execute()
    assert len(sess["session"]) == 2
    with pytest.raises(FitCompatError):
        users.dataset().aggregate(userId="12345", body={})
