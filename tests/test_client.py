from __future__ import annotations

import datetime as dt
import json
import time

import httpx
import pytest

from google_health import (
    DataType,
    FailedPrecondition,
    HealthClient,
    InvalidArgument,
    NotFound,
    OAuthCredentials,
    PermissionDenied,
    RateLimited,
    ServerError,
    operation_result,
    time_range_filter,
)
from google_health.errors import APIError


# ---------------------------------------------------------------------------- filters
def test_filters_per_kind() -> None:
    assert time_range_filter("steps", "2026-10-01", "2026-10-02") == (
        'steps.interval.start_time >= "2026-10-01T00:00:00Z" AND '
        'steps.interval.start_time < "2026-10-02T00:00:00Z"')
    assert time_range_filter(DataType.ACTIVE_ENERGY_BURNED, start="2026-10-01", civil=True) == (
        'active_energy_burned.interval.civil_start_time >= "2026-10-01"')
    assert time_range_filter("weight", dt.datetime(2026, 1, 1, 12), None) == (
        'weight.sample_time.physical_time >= "2026-01-01T12:00:00Z"')
    assert time_range_filter("weight", "2026-01-01", "2026-01-02", civil=True).startswith(
        'weight.sample_time.civil_time >= "2026-01-01"')
    assert time_range_filter("daily-heart-rate-variability", dt.date(2026, 8, 1),
                             dt.date(2026, 8, 15)) == (
        'daily_heart_rate_variability.date >= "2026-08-01" AND '
        'daily_heart_rate_variability.date < "2026-08-15"')
    assert time_range_filter("sleep", "2026-10-01", "2026-10-02", civil=True) == (
        'sleep.interval.civil_end_time >= "2026-10-01" AND '
        'sleep.interval.civil_end_time < "2026-10-02"')
    assert time_range_filter("exercise", dt.datetime(2026, 10, 1, 6, 30)) == (
        'exercise.interval.civil_start_time >= "2026-10-01T06:30:00"')
    with pytest.raises(ValueError):
        time_range_filter("electrocardiogram", "2026-10-01", "2026-10-02")
    with pytest.raises(ValueError):
        time_range_filter("total-calories", "2026-10-01")


def test_aware_datetime_converted_to_utc() -> None:
    ist = dt.timezone(dt.timedelta(hours=3))
    f = time_range_filter("steps", dt.datetime(2026, 10, 1, 3, tzinfo=ist))
    assert '"2026-10-01T00:00:00Z"' in f


# ---------------------------------------------------------------------------- list
def test_list_paginates_and_sends_filter(api, client: HealthClient) -> None:
    pages = [
        httpx.Response(200, json={"dataPoints": [{"steps": {"count": "1"}}],
                                  "nextPageToken": "p2"}),
        httpx.Response(200, json={"dataPoints": [{"steps": {"count": "2"}}]}),
    ]
    api.add("GET", "/v4/users/me/dataTypes/steps/dataPoints", pages)
    out = list(client.data_points.list("steps", "2026-10-01", "2026-10-02",
                                       data_source_family="google-wearables", page_size=1))
    assert [p["steps"]["count"] for p in out] == ["1", "2"]
    first, second = api.requests
    assert first.headers["Authorization"] == "Bearer tok"
    assert first.url.params["filter"].startswith("steps.interval.start_time >=")
    assert first.url.params["dataSourceFamily"] == "users/me/dataSourceFamilies/google-wearables"
    assert first.url.params["pageSize"] == "1"
    assert "pageToken" not in first.url.params
    assert second.url.params["pageToken"] == "p2"


def test_list_limit_stops_early(api, client) -> None:
    api.add("GET", "/dataPoints", {"dataPoints": [{"a": 1}, {"a": 2}, {"a": 3}],
                                    "nextPageToken": "x"})
    assert len(list(client.data_points.list("weight", limit=2))) == 2
    assert len(api.requests) == 1


def test_reconcile_path(api, client) -> None:
    api.add("GET", "/dataPoints:reconcile", {"dataPoints": []})
    list(client.data_points.reconcile("heart-rate", "2026-10-01", "2026-10-02", user="123"))
    assert api.requests[0].url.path == "/v4/users/123/dataTypes/heart-rate/dataPoints:reconcile"


# ---------------------------------------------------------------------------- roll-ups
def _rollup_responder(api):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "range" in body and "start" in body["range"]:
            d = body["range"]["start"]["date"]
            pts = [{"civilStartTime": {"date": d}, "steps": {"countSum": "100"}}]
        else:
            pts = [{"startTime": body["range"]["startTime"], "endTime": body["range"]["endTime"],
                    "heartRate": {"beatsPerMinuteAvg": 60.5}}]
        return httpx.Response(200, json={"rollupDataPoints": pts})
    return handler


def test_daily_roll_up_chunks_long_ranges(api, client) -> None:
    api.add("POST", ":dailyRollUp", _rollup_responder(api))
    vals = client.daily("steps", dt.date(2026, 1, 1), dt.date(2026, 4, 11))  # 100 days
    bodies = [json.loads(r.content) for r in api.requests]
    assert len(bodies) == 2
    assert bodies[0]["range"] == {"start": {"date": {"year": 2026, "month": 1, "day": 1}},
                                  "end": {"date": {"year": 2026, "month": 4, "day": 1}}}
    assert bodies[1]["range"]["end"]["date"] == {"year": 2026, "month": 4, "day": 11}
    assert bodies[0]["windowSizeDays"] == 1
    assert [v.value for v in vals] == [100.0, 100.0]
    assert vals[0].date == dt.date(2026, 1, 1)


def test_daily_roll_up_short_range_types_and_window_alignment(api, client) -> None:
    api.add("POST", ":dailyRollUp", _rollup_responder(api))
    list(client.data_points.daily_roll_up("total-calories", "2026-01-01", "2026-01-31",
                                          window_days=4))
    spans = []
    for r in api.requests:
        b = json.loads(r.content)["range"]
        s = dt.date(**b["start"]["date"])
        e = dt.date(**b["end"]["date"])
        spans.append((e - s).days)
    assert spans == [12, 12, 6]  # 14-day limit rounded down to a multiple of 4
    with pytest.raises(ValueError):
        list(client.data_points.daily_roll_up("total-calories", "2026-01-01", "2026-02-01",
                                              window_days=15))
    with pytest.raises(ValueError):
        list(client.data_points.daily_roll_up("sleep", "2026-01-01", "2026-01-02"))


def test_roll_up_physical_chunks_and_page_token_in_body(api, client) -> None:
    calls = []

    def handler(request):
        b = json.loads(request.content)
        calls.append(b)
        if "pageToken" not in b and len(calls) == 1:
            return httpx.Response(200, json={"rollupDataPoints": [], "nextPageToken": "t"})
        return httpx.Response(200, json={"rollupDataPoints": []})

    api.add("POST", ":rollUp", handler)
    start = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    list(client.data_points.roll_up("heart-rate", start, start + dt.timedelta(days=20),
                                    dt.timedelta(hours=1)))
    assert calls[0]["windowSize"] == "3600s"
    assert calls[0]["range"] == {"startTime": "2026-10-01T00:00:00Z",
                                 "endTime": "2026-10-15T00:00:00Z"}
    assert calls[1]["pageToken"] == "t"
    assert calls[2]["range"]["startTime"] == "2026-10-15T00:00:00Z"
    assert len(calls) == 3


def test_windows_helper(api, client) -> None:
    api.add("POST", ":rollUp", _rollup_responder(api))
    w = client.windows("heart-rate", "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", 900)
    assert w[0].value == 60.5
    assert w[0].start == dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------- writes
def test_create_wraps_payload_and_operation_result(api, client) -> None:
    api.add("POST", "/dataTypes/weight/dataPoints",
            {"name": "op1", "done": True, "response": {"name": "users/me/x"}})
    sample = {"physicalTime": "2026-10-01T07:00:00Z"}
    op = client.data_points.create("weight", {"weightGrams": 72500, "sampleTime": sample})
    assert api.json_body()["weight"]["weightGrams"] == 72500
    assert operation_result(op) == {"name": "users/me/x"}
    with pytest.raises(APIError):
        operation_result({"done": True, "error": {"code": 3, "message": "bad"}})


def test_patch_and_batch_delete(api, client) -> None:
    api.add("PATCH", "/dataPoints/abc1", {"done": True})
    api.add("POST", ":batchDelete", {"done": True})
    client.data_points.patch("sleep", "abc1", {"sleep": {"type": "CLASSIC"}})
    assert api.json_body()["name"] == "users/me/dataTypes/sleep/dataPoints/abc1"
    client.data_points.batch_delete("sleep", ["abc1", "users/me/dataTypes/sleep/dataPoints/z9"])
    assert api.json_body()["names"] == ["users/me/dataTypes/sleep/dataPoints/abc1",
                                        "users/me/dataTypes/sleep/dataPoints/z9"]


def test_export_tcx_returns_bytes(api, client) -> None:
    api.add("GET", ":exportExerciseTcx",
            lambda r: httpx.Response(200, content=b"<TrainingCenterDatabase/>"))
    data = client.data_points.export_exercise_tcx("42", partial_data=True)
    assert data == b"<TrainingCenterDatabase/>"
    r = api.requests[0]
    assert r.url.path == "/v4/users/me/dataTypes/exercise/dataPoints/42:exportExerciseTcx"
    assert r.url.params["alt"] == "media" and r.url.params["partialData"] == "true"


def test_users_and_devices(api, client) -> None:
    api.add("GET", "/identity", {"healthUserId": "h1", "legacyUserId": "ABC"})
    api.add("PATCH", "/settings", {"weightUnit": "WEIGHT_UNIT_KILOGRAMS"})
    api.add("GET", "/pairedDevices", {"pairedDevices": [{"name": "users/me/pairedDevices/1"}]})
    assert client.identity()["legacyUserId"] == "ABC"
    client.users.update_settings({"weightUnit": "WEIGHT_UNIT_KILOGRAMS"})
    assert api.requests[-1].url.params["updateMask"] == "weightUnit"
    assert len(client.devices.list()) == 1


def test_subscribers(api, client) -> None:
    api.add("POST", "/projects/p1/subscribers", {"name": "op"})
    api.add("POST", "/subscriptions", {"name": "s"})
    client.subscribers.create("https://x.example/h", "Bearer s3cret", ["steps", "sleep"],
                              subscriber_id="my-sub", project="p1")
    body = api.json_body()
    assert body["endpointAuthorization"] == {"secret": "Bearer s3cret"}
    assert body["subscriberConfigs"][0]["dataTypes"] == ["steps", "sleep"]
    assert api.requests[-1].url.params["subscriberId"] == "my-sub"
    client.subscribers.subscribe_user("my-sub", "777", ["steps"], project="p1")
    assert api.json_body() == {"user": "users/777", "dataTypes": ["users/777/dataTypes/steps"]}
    with pytest.raises(ValueError):
        client.subscribers.list()


# ---------------------------------------------------------------------------- errors & retries
@pytest.mark.parametrize("status,code,exc", [
    (400, "INVALID_ARGUMENT", InvalidArgument),
    (403, "PERMISSION_DENIED", PermissionDenied),
    (404, "NOT_FOUND", NotFound),
    (400, "FAILED_PRECONDITION", FailedPrecondition),
])
def test_error_mapping(api, client, status, code, exc) -> None:
    api.add("GET", "/profile", lambda r: httpx.Response(status, json={"error": {
        "code": status, "status": code, "message": "nope",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "SCOPE_NOT_GRANTED"}]}}))
    with pytest.raises(exc) as ei:
        client.users.get_profile()
    assert ei.value.reason == "SCOPE_NOT_GRANTED"
    assert ei.value.message == "nope"


def test_retries_transient_errors(api, client) -> None:
    api.add("GET", "/profile", [httpx.Response(503),
                                httpx.Response(429, headers={"Retry-After": "0"}),
                                httpx.Response(200, json={"age": 30})])
    assert client.users.get_profile() == {"age": 30}
    assert len(api.requests) == 3


def test_gives_up_after_max_retries(api, client) -> None:
    api.add("GET", "/profile", lambda r: httpx.Response(429, json={"error": {
        "status": "RESOURCE_EXHAUSTED", "message": "slow down"}}))
    with pytest.raises(RateLimited):
        client.users.get_profile()
    assert len(api.requests) == 4


def test_non_idempotent_not_retried_on_500(api, client) -> None:
    api.add("POST", "/dataTypes/weight/dataPoints", lambda r: httpx.Response(500))
    with pytest.raises(ServerError):
        client.data_points.create("weight", {"weightGrams": 1})
    assert len(api.requests) == 1


def test_oauth_refresh_and_401_retry(api) -> None:
    refreshed = []
    tokens = iter(["fresh1", "fresh2"])

    def token_endpoint(request):
        form = dict(x.split("=") for x in request.content.decode().split("&"))
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "r1"
        return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})

    seen = []

    def profile(request):
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        return httpx.Response(200, json={"ok": True})

    api.add("POST", "/token", token_endpoint)
    api.add("GET", "/profile", profile)
    creds = OAuthCredentials(access_token="old", refresh_token="r1", client_id="cid",
                             client_secret="sec", expires_at=time.time() - 10,
                             on_refresh=lambda c: refreshed.append(c.access_token))
    c = HealthClient(creds, http=httpx.Client(transport=httpx.MockTransport(api)))
    assert c.users.get_profile() == {"ok": True}
    assert seen == ["Bearer fresh1", "Bearer fresh2"]
    assert refreshed == ["fresh1", "fresh2"]


def test_credentials_roundtrip(tmp_path) -> None:
    p = tmp_path / "t.json"
    OAuthCredentials(access_token="a", refresh_token="r", client_id="c", expires_at=5.0,
                     scopes=("s1",)).save(p)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    c = OAuthCredentials.load(p)
    assert c.refresh_token == "r" and c.scopes == ("s1",) and c.expired
    c._apply({"access_token": "new", "expires_in": 100})
    assert json.loads(p.read_text())["access_token"] == "new"
