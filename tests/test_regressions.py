"""Regression tests for issues found in code review."""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from google_health import OAuthCredentials, time_range_filter
from google_health._api import _window_timedelta, batch_delete, wrap_payload
from google_health.fitbit_compat import Fitbit
from google_health.webhooks import SignatureError, SignatureVerifier
from tests.test_webhooks import BODY, make_key, sign


@pytest.mark.parametrize("dtype,inner", [
    ("vo2-max", {"vo2Max": 45.2}),
    ("run-vo2-max", {"runVo2Max": 50.1}),
    ("moods", {"moods": ["HAPPY"]}),
    ("active-zone-minutes", {"activeZoneMinutes": "2"}),
])
def test_inner_payload_with_same_named_scalar_is_wrapped(dtype, inner) -> None:
    from google_health import DataType
    key = DataType(dtype).camel
    assert wrap_payload(dtype, inner) == {key: inner}
    assert wrap_payload(dtype, {key: inner}) == {key: inner}


def test_activity_level_is_interval() -> None:
    assert time_range_filter("activity-level", "2026-10-01").startswith(
        "activity_level.interval.start_time")


def test_session_filters_reject_absolute_instants() -> None:
    with pytest.raises(ValueError):
        time_range_filter("exercise", "2026-10-01T00:00:00Z")
    with pytest.raises(ValueError):
        time_range_filter("exercise", dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    assert "civil_start_time" in time_range_filter("exercise", "2026-10-01T06:00:00")


def test_window_parsing_and_batch_delete_limits() -> None:
    assert _window_timedelta("15m") == dt.timedelta(minutes=15)
    assert _window_timedelta("1h") == dt.timedelta(hours=1)
    assert _window_timedelta("900s") == dt.timedelta(seconds=900)
    with pytest.raises(ValueError):
        _window_timedelta("soon")
    with pytest.raises(ValueError):
        batch_delete("me", "sleep", [])
    with pytest.raises(ValueError):
        batch_delete("me", "sleep", [f"id{i}" for i in range(10_001)])


def test_limit_zero(api, client) -> None:
    api.add("GET", "/dataPoints", {"dataPoints": [{"a": 1}]})
    assert list(client.data_points.list("weight", limit=0)) == []
    assert api.requests == []


def test_negative_retry_after(api, client) -> None:
    api.add("GET", "/profile", [httpx.Response(429, headers={"Retry-After": "-5"}),
                                httpx.Response(200, json={})])
    sleeps = []
    client._sleep = sleeps.append
    client.users.get_profile()
    assert sleeps == [0.0]


def test_keyset_fetch_failure_keeps_cached_keys() -> None:
    priv, entry = make_key(5)
    calls = []

    def fetch(url):
        calls.append(1)
        if len(calls) > 1:
            raise httpx.ConnectError("down")
        return {"key": [entry]}

    v = SignatureVerifier(fetch=fetch, ttl=0)
    v.verify(BODY, sign(priv, 5, BODY))
    v.verify(BODY, sign(priv, 5, BODY))  # refresh fails, cached key still used
    assert len(calls) == 2
    with pytest.raises(SignatureError):
        SignatureVerifier(fetch=fetch).verify(BODY, sign(priv, 5, BODY))


def test_invalidate_ignores_stale_token() -> None:
    c = OAuthCredentials(access_token="new", expires_at=9e12)
    c.invalidate("old")
    assert not c.expired
    c.invalidate("new")
    assert c.expired


def test_save_fixes_permissions(tmp_path) -> None:
    p = tmp_path / "t.json"
    p.write_text("{}")
    p.chmod(0o644)
    OAuthCredentials(access_token="a").save(p)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert json.loads(p.read_text())["access_token"] == "a"
    assert [x.name for x in tmp_path.iterdir()] == ["t.json"]


def test_compat_kwargs_and_activities_default(api, client) -> None:
    Fitbit("c", "s", oauth2=True, requests_kwargs={}, timeout=5)  # no TypeError
    api.add("GET", "/settings", {"timeZone": "Pacific/Kiritimati"})  # UTC+14
    api.add("POST", ":dailyRollUp", {"rollupDataPoints": []})
    out = Fitbit("c", None, client=client).activities()
    assert out["summary"]["steps"] == 0
    body = json.loads(api.requests[-1].content)
    from zoneinfo import ZoneInfo
    today = dt.datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    assert dt.date(**body["range"]["start"]["date"]) == today


def test_intraday_inclusive_end_uses_detail_step(api, client) -> None:
    api.add("GET", "/settings", {"timeZone": "UTC"})
    api.add("POST", ":rollUp", {"rollupDataPoints": []})
    Fitbit("c", None, client=client).intraday_time_series(
        "activities/steps", base_date="2026-10-28", detail_level="15min",
        start_time="00:00", end_time="01:00")
    rng = json.loads(api.requests[-1].content)["range"]
    assert rng == {"startTime": "2026-10-28T00:00:00Z", "endTime": "2026-10-28T01:15:00Z"}


def test_classic_sleep_keys() -> None:
    from google_health.fitbit_compat import _legacy_sleep
    s = _legacy_sleep({"sleep": {"type": "CLASSIC", "summary": {"stagesSummary": [
        {"type": "AWAKE", "count": "2", "minutes": "10"}]}}}, dt.date(2026, 10, 28))
    assert s["levels"]["summary"] == {"awake": {"count": 2, "minutes": 10}}
