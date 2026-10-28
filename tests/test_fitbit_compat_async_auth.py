from __future__ import annotations

import datetime as dt
import json
import urllib.parse

import httpx
import pytest

from google_health import OAuthFlow
from google_health.cli import build_parser
from google_health.fitbit_compat import METRIC, US, Fitbit


def _daily(api, values):
    def handler(request):
        b = json.loads(request.content)
        s = dt.date(**b["range"]["start"]["date"])
        e = dt.date(**b["range"]["end"]["date"])
        pts = []
        d = s
        while d < e:
            if d.isoformat() in values:
                pts.append({"civilStartTime": {"date": {"year": d.year, "month": d.month,
                                                        "day": d.day}},
                            **values[d.isoformat()]})
            d += dt.timedelta(days=1)
        return httpx.Response(200, json={"rollupDataPoints": pts})
    api.add("POST", ":dailyRollUp", handler)


def test_time_series_steps_period(api, client) -> None:
    _daily(api, {"2026-10-26": {"steps": {"countSum": "1200"}},
                 "2026-10-28": {"steps": {"countSum": "8421"}}})
    fb = Fitbit("cid", "sec", client=client)
    out = fb.time_series("activities/steps", base_date="2026-10-28", period="7d")
    series = out["activities-steps"]
    assert [x["dateTime"] for x in series][0] == "2026-10-22"
    assert series[-1] == {"dateTime": "2026-10-28", "value": "8421"}
    assert series[-3]["value"] == "1200" and series[-2]["value"] == "0"
    with pytest.raises(TypeError):
        fb.time_series("activities/steps", period="7d", end_date="2026-10-28")
    with pytest.raises(ValueError):
        fb.time_series("activities/unknown")


def test_tracker_resource_uses_wearables_family(api, client) -> None:
    _daily(api, {})
    Fitbit("c", None, client=client).time_series("activities/tracker/steps",
                                                  base_date="2026-10-01")
    assert json.loads(api.requests[0].content)["dataSourceFamily"].endswith("google-wearables")


@pytest.mark.parametrize("system,expected", [(METRIC, "10"), (US, "6.21")])
def test_distance_units(api, client, system, expected) -> None:
    _daily(api, {"2026-10-28": {"distance": {"millimetersSum": "10000000"}}})
    fb = Fitbit("c", None, client=client, system=system)
    out = fb.time_series("activities/distance", base_date="2026-10-28", end_date="2026-10-28")
    assert out["activities-distance"][0]["value"] == expected


def test_resting_heart_rate(api, client) -> None:
    api.add("GET", "/daily-resting-heart-rate/dataPoints", {"dataPoints": [
        {"dailyRestingHeartRate": {"date": {"year": 2026, "month": 10, "day": 27},
                                   "beatsPerMinute": "58"}}]})
    out = Fitbit("c", None, client=client).time_series(
        "activities/heart", base_date="2026-10-27", end_date="2026-10-28")
    assert out["activities-heart"][0]["value"]["restingHeartRate"] == 58
    assert "restingHeartRate" not in out["activities-heart"][1]["value"]
    assert "daily_resting_heart_rate.date" in api.requests[0].url.params["filter"]


def test_get_sleep(api, client) -> None:
    api.add("GET", "/sleep/dataPoints", {"dataPoints": [{
        "name": "users/me/dataTypes/sleep/dataPoints/s1",
        "sleep": {
            "type": "STAGES",
            "metadata": {"mainSleep": True},
            "interval": {"startTime": "2026-10-27T20:00:00Z", "endTime": "2026-10-28T04:00:00Z",
                         "civilStartTime": {"date": {"year": 2026, "month": 10, "day": 27},
                                            "time": {"hours": 23}},
                         "civilEndTime": {"date": {"year": 2026, "month": 10, "day": 28},
                                          "time": {"hours": 7}}},
            "summary": {"minutesAsleep": "420", "minutesAwake": "60",
                        "minutesInSleepPeriod": "480",
                        "stagesSummary": [{"type": "DEEP", "count": "4", "minutes": "80"}]},
        }}]})
    out = Fitbit("c", None, client=client).get_sleep("2026-10-28")
    s = out["sleep"][0]
    assert s["logId"] == "s1" and s["isMainSleep"] and s["type"] == "stages"
    assert s["duration"] == 8 * 3600 * 1000 and s["efficiency"] == 88
    assert s["startTime"] == "2026-10-27T23:00:00.000"
    assert s["levels"]["summary"]["deep"] == {"count": 4, "minutes": 80}
    assert out["summary"] == {"totalMinutesAsleep": 420, "totalSleepRecords": 1,
                              "totalTimeInBed": 480}
    flt = api.requests[0].url.params["filter"]
    assert flt.startswith('sleep.interval.civil_end_time >= "2026-10-28"')


def test_intraday_uses_user_timezone(api, client) -> None:
    api.add("GET", "/settings", {"timeZone": "Europe/Istanbul", "utcOffset": "10800s"})
    bodies = []

    def roll(request):
        b = json.loads(request.content)
        bodies.append(b)
        return httpx.Response(200, json={"rollupDataPoints": [
            {"startTime": "2026-10-28T06:00:00Z", "endTime": "2026-10-28T06:15:00Z",
             "steps": {"countSum": "150"}},
            {"startTime": "2026-10-28T06:15:00Z", "endTime": "2026-10-28T06:30:00Z"},
        ]})

    api.add("POST", ":rollUp", roll)
    out = Fitbit("c", None, client=client).intraday_time_series(
        "activities/steps", base_date="2026-10-28", detail_level="15min",
        start_time="09:00", end_time="09:59")
    assert bodies[0]["range"] == {"startTime": "2026-10-28T06:00:00Z",
                                  "endTime": "2026-10-28T07:00:00Z"}
    assert bodies[0]["windowSize"] == "900s"
    intr = out["activities-steps-intraday"]
    assert intr["dataset"] == [{"time": "09:00:00", "value": 150}]
    assert intr["datasetInterval"] == 15
    assert out["activities-steps"][0]["value"] == "150"


def test_profile_and_devices(api, client) -> None:
    api.add("GET", "/identity", {"healthUserId": "h", "legacyUserId": "2ABCDE"})
    api.add("GET", "/profile", {"age": 34, "membershipStartDate": {"year": 2019, "month": 5,
                                                                    "day": 1},
                                "autoWalkingStrideLengthMm": 755})
    api.add("GET", "/settings", {"timeZone": "Europe/Istanbul", "utcOffset": "10800s"})
    api.add("GET", "/pairedDevices", {"pairedDevices": [
        {"name": "users/me/pairedDevices/d1", "batteryStatus": "HIGH", "deviceType": "TRACKER"}]})
    fb = Fitbit("c", None, client=client)
    u = fb.user_profile_get()["user"]
    assert u["encodedId"] == "2ABCDE" and u["offsetFromUTCMillis"] == 10800000
    assert u["memberSince"] == "2019-05-01" and u["strideLengthWalking"] == 75.5
    assert fb.get_devices()[0]["id"] == "d1"
    assert fb.legacy_user_id() == "2ABCDE"


async def test_async_client(api, aclient) -> None:
    api.add("GET", "/weight/dataPoints", [
        httpx.Response(200, json={"dataPoints": [{"weight": {"weightGrams": 70000}}],
                                  "nextPageToken": "n"}),
        httpx.Response(200, json={"dataPoints": [{"weight": {"weightGrams": 71000}}]})])
    api.add("POST", ":dailyRollUp", {"rollupDataPoints": [
        {"civilStartTime": {"date": {"year": 2026, "month": 10, "day": 1}},
         "weight": {"weightGramsAvg": 70500}}]})
    api.add("GET", "/identity", [httpx.Response(503),
                                 httpx.Response(200, json={"healthUserId": "x"})])
    got = [p async for p in aclient.data_points.list("weight", "2026-10-01", "2026-10-02")]
    assert [p["weight"]["weightGrams"] for p in got] == [70000, 71000]
    days = await aclient.daily("weight", "2026-10-01", "2026-10-02")
    assert days[0].value == 70.5
    assert (await aclient.identity()) == {"healthUserId": "x"}
    await aclient.aclose()


def test_oauth_flow_url_and_exchange(api) -> None:
    flow = OAuthFlow("cid", "sec", ["s1", "s2"], redirect_uri="https://app/cb")
    req = flow.authorization_url()
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.url).query))
    assert q["scope"] == "s1 s2" and q["access_type"] == "offline"
    assert q["code_challenge_method"] == "S256" and q["state"] == req.state
    assert req.code_verifier and len(req.code_verifier) >= 43

    def token(request):
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert form["code"] == "c0de" and form["code_verifier"] == req.code_verifier
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r",
                                         "expires_in": 3599, "scope": "s1 s2"})

    api.add("POST", "/token", token)
    creds = flow.exchange_code("c0de", req.code_verifier,
                               http=httpx.Client(transport=httpx.MockTransport(api)))
    assert creds.refresh_token == "r" and creds.scopes == ("s1", "s2") and not creds.expired


def test_cli_parser() -> None:
    args = build_parser().parse_args(["daily", "steps", "--days", "3"])
    assert args.data_type == "steps" and args.days == 3
