"""Transport-agnostic request builders shared by the sync and async clients."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from . import _time
from ._version import __version__
from .errors import APIError
from .filters import time_range_filter
from .types import (
    DAILY_ROLLUP_TYPES,
    ROLLUP_TYPES,
    DataSourceFamily,
    DataType,
    max_rollup_days,
)

DEFAULT_BASE_URL = "https://health.googleapis.com/v4/"
USER_AGENT = f"google-health-python/{__version__}"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

DataTypeLike = Union[DataType, str]
FamilyLike = Union[DataSourceFamily, str, None]


@dataclass
class Call:
    method: str
    path: str
    params: Optional[Dict[str, Any]] = None
    json: Any = None
    #: Safe to retry on 5xx (reads and read-only POSTs such as roll-ups).
    idempotent: bool = True
    #: Return raw bytes instead of JSON.
    raw: bool = False


@dataclass
class PagedCall:
    call: Call
    items_key: str
    #: Where the page token goes: "params" (GET) or "json" (POST body).
    token_in: str = "params"
    limit: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def with_token(self, token: str) -> Call:
        c = self.call
        if self.token_in == "params":
            params = dict(c.params or {})
            params["pageToken"] = token
            return Call(c.method, c.path, params, c.json, c.idempotent, c.raw)
        body = dict(c.json or {})
        body["pageToken"] = token
        return Call(c.method, c.path, c.params, body, c.idempotent, c.raw)


def clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# --------------------------------------------------------------------------- names
def user_name(user: str) -> str:
    user = str(user)
    return user if user.startswith("users/") else f"users/{user}"


def data_type_parent(user: str, data_type: DataTypeLike) -> str:
    return f"{user_name(user)}/dataTypes/{DataType(data_type).value}"


def data_point_name(user: str, data_type: DataTypeLike, point_id: str) -> str:
    if point_id.startswith("users/"):
        return point_id
    return f"{data_type_parent(user, data_type)}/dataPoints/{point_id}"


def family_name(user: str, family: FamilyLike) -> Optional[str]:
    if family is None:
        return None
    f = str(family.value if isinstance(family, DataSourceFamily) else family)
    if f.startswith("users/"):
        return f
    return f"{user_name(user)}/dataSourceFamilies/{f}"


def project_name(project: str) -> str:
    project = str(project)
    return project if project.startswith("projects/") else f"projects/{project}"


def subscriber_name(project: str, subscriber: str) -> str:
    if subscriber.startswith("projects/"):
        return subscriber
    return f"{project_name(project)}/subscribers/{subscriber}"


def update_mask(fields: Union[str, Sequence[str], None], body: Dict[str, Any]) -> str:
    if fields is None:
        return ",".join(k for k in body if k != "name")
    if isinstance(fields, str):
        return fields
    return ",".join(fields)


# --------------------------------------------------------------------------- users
def get_identity(user: str) -> Call:
    return Call("GET", f"{user_name(user)}/identity")


def get_profile(user: str) -> Call:
    return Call("GET", f"{user_name(user)}/profile")


def update_profile(user: str, profile: Dict[str, Any], fields: Any) -> Call:
    return Call("PATCH", f"{user_name(user)}/profile",
                {"updateMask": update_mask(fields, profile)}, profile, idempotent=True)


def get_settings(user: str) -> Call:
    return Call("GET", f"{user_name(user)}/settings")


def update_settings(user: str, settings: Dict[str, Any], fields: Any) -> Call:
    return Call("PATCH", f"{user_name(user)}/settings",
                {"updateMask": update_mask(fields, settings)}, settings, idempotent=True)


def get_irn_profile(user: str) -> Call:
    return Call("GET", f"{user_name(user)}/irnProfile")


# --------------------------------------------------------------------------- devices
def list_devices(user: str, page_size: Optional[int]) -> PagedCall:
    return PagedCall(
        Call("GET", f"{user_name(user)}/pairedDevices", clean({"pageSize": page_size})),
        "pairedDevices",
    )


def get_device(user: str, device_id: str) -> Call:
    name = device_id if device_id.startswith("users/") else (
        f"{user_name(user)}/pairedDevices/{device_id}")
    return Call("GET", name)


# --------------------------------------------------------------------------- data points
def list_data_points(
    user: str,
    data_type: DataTypeLike,
    start: Optional[_time.DateTimeLike],
    end: Optional[_time.DateTimeLike],
    *,
    civil: bool,
    filter: Optional[str],
    family: FamilyLike,
    page_size: Optional[int],
    limit: Optional[int],
    reconcile: bool = False,
) -> PagedCall:
    if filter is None and (start is not None or end is not None):
        filter = time_range_filter(data_type, start, end, civil=civil)
    suffix = "dataPoints:reconcile" if reconcile else "dataPoints"
    params = clean({
        "filter": filter,
        "dataSourceFamily": family_name(user, family),
        "pageSize": page_size,
    })
    return PagedCall(Call("GET", f"{data_type_parent(user, data_type)}/{suffix}", params),
                     "dataPoints", limit=limit)


def get_data_point(user: str, data_type: DataTypeLike, point_id: str) -> Call:
    return Call("GET", data_point_name(user, data_type, point_id))


def create_data_point(user: str, data_type: DataTypeLike, data_point: Dict[str, Any]) -> Call:
    body = wrap_payload(data_type, data_point)
    return Call("POST", f"{data_type_parent(user, data_type)}/dataPoints", None, body,
                idempotent=False)


def patch_data_point(user: str, data_type: DataTypeLike, point_id: str,
                     data_point: Dict[str, Any]) -> Call:
    name = data_point_name(user, data_type, point_id)
    body = dict(wrap_payload(data_type, data_point))
    body["name"] = name
    return Call("PATCH", name, None, body, idempotent=True)


def batch_delete(user: str, data_type: DataTypeLike, ids: Iterable[str]) -> Call:
    names = [data_point_name(user, data_type, i) for i in ids]
    return Call("POST", f"{data_type_parent(user, data_type)}/dataPoints:batchDelete",
                None, {"names": names}, idempotent=True)


def export_exercise_tcx(user: str, exercise_id: str, partial_data: bool) -> Call:
    name = data_point_name(user, DataType.EXERCISE, exercise_id)
    params: Dict[str, Any] = {"alt": "media"}
    if partial_data:
        params["partialData"] = "true"
    return Call("GET", f"{name}:exportExerciseTcx", params, raw=True)


def wrap_payload(data_type: DataTypeLike, data_point: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either a full DataPoint (``{"steps": {...}}``) or just the inner payload."""
    key = DataType(data_type).camel
    if key in data_point:
        return data_point
    return {key: data_point}


def roll_up(
    user: str,
    data_type: DataTypeLike,
    start: _time.DateTimeLike,
    end: _time.DateTimeLike,
    window: _time.DurationLike,
    *,
    family: FamilyLike,
    page_size: Optional[int],
) -> List[PagedCall]:
    dtype = DataType(data_type)
    if dtype not in ROLLUP_TYPES:
        raise ValueError(f"{dtype.value} does not support rollUp")
    s, e = _time.to_utc_datetime(start), _time.to_utc_datetime(end)
    if e <= s:
        raise ValueError("end must be after start")
    win = _window_timedelta(window)
    out = []
    for cs, ce in _time.chunk_datetimes(s, e, dt.timedelta(days=max_rollup_days(dtype)), win):
        body = clean({
            "range": {"startTime": _time.rfc3339(cs), "endTime": _time.rfc3339(ce)},
            "windowSize": _time.duration(win),
            "dataSourceFamily": family_name(user, family),
            "pageSize": page_size,
        })
        out.append(PagedCall(
            Call("POST", f"{data_type_parent(user, dtype)}/dataPoints:rollUp", None, body),
            "rollupDataPoints", token_in="json"))
    return out


def daily_roll_up(
    user: str,
    data_type: DataTypeLike,
    start: _time.DateLike,
    end: _time.DateLike,
    window_days: int,
    *,
    family: FamilyLike,
    page_size: Optional[int],
) -> List[PagedCall]:
    dtype = DataType(data_type)
    if dtype not in DAILY_ROLLUP_TYPES:
        raise ValueError(f"{dtype.value} does not support dailyRollUp")
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    s, e = _time.to_date(start), _time.to_date(end)
    if e <= s:
        raise ValueError("end must be after start (end is exclusive)")
    max_days = max_rollup_days(dtype)
    if window_days > max_days:
        raise ValueError(f"window_days exceeds the {max_days}-day limit for {dtype.value}")
    span = (max_days // window_days) * window_days
    out = []
    for cs, ce in _time.chunk_dates(s, e, span):
        body = clean({
            "range": {"start": _time.civil_datetime_message(cs),
                      "end": _time.civil_datetime_message(ce)},
            "windowSizeDays": window_days,
            "dataSourceFamily": family_name(user, family),
            "pageSize": page_size,
        })
        out.append(PagedCall(
            Call("POST", f"{data_type_parent(user, dtype)}/dataPoints:dailyRollUp", None, body),
            "rollupDataPoints", token_in="json"))
    return out


def _window_timedelta(window: _time.DurationLike) -> dt.timedelta:
    if isinstance(window, dt.timedelta):
        return window
    if isinstance(window, (int, float)):
        return dt.timedelta(seconds=window)
    td = _time.parse_duration(window if window.endswith("s") else window + "s")
    assert td is not None
    return td


# --------------------------------------------------------------------------- subscribers
def create_subscriber(project: str, endpoint_uri: str, secret: str,
                      data_types: Sequence[DataTypeLike], policy: str,
                      subscriber_id: Optional[str]) -> Call:
    body = {
        "endpointUri": endpoint_uri,
        "endpointAuthorization": {"secret": secret},
        "subscriberConfigs": [{
            "dataTypes": [DataType(d).value for d in data_types],
            "subscriptionCreatePolicy": policy,
        }],
    }
    return Call("POST", f"{project_name(project)}/subscribers",
                clean({"subscriberId": subscriber_id}), body, idempotent=False)


def list_subscribers(project: str, page_size: Optional[int]) -> PagedCall:
    return PagedCall(Call("GET", f"{project_name(project)}/subscribers",
                          clean({"pageSize": page_size})), "subscribers")


def patch_subscriber(project: str, subscriber: str, body: Dict[str, Any], fields: Any) -> Call:
    name = subscriber_name(project, subscriber)
    return Call("PATCH", name, {"updateMask": update_mask(fields, body)}, body)


def delete_subscriber(project: str, subscriber: str, force: bool) -> Call:
    return Call("DELETE", subscriber_name(project, subscriber),
                {"force": "true"} if force else None)


def create_subscription(project: str, subscriber: str, user: str,
                        data_types: Sequence[DataTypeLike],
                        subscription_id: Optional[str]) -> Call:
    u = user_name(user)
    body = {"user": u, "dataTypes": [f"{u}/dataTypes/{DataType(d).value}" for d in data_types]}
    return Call("POST", f"{subscriber_name(project, subscriber)}/subscriptions",
                clean({"subscriptionId": subscription_id}), body, idempotent=False)


def list_subscriptions(project: str, subscriber: str, filter: Optional[str],
                       page_size: Optional[int]) -> PagedCall:
    return PagedCall(Call("GET", f"{subscriber_name(project, subscriber)}/subscriptions",
                          clean({"filter": filter, "pageSize": page_size})), "subscriptions")


def patch_subscription(name: str, user: str, data_types: Sequence[DataTypeLike]) -> Call:
    u = user_name(user)
    body = {"name": name,
            "dataTypes": [f"{u}/dataTypes/{DataType(d).value}" for d in data_types]}
    return Call("PATCH", name, {"updateMask": "dataTypes"}, body)


def delete_subscription(name: str) -> Call:
    return Call("DELETE", name)


# --------------------------------------------------------------------------- operations
def operation_result(op: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return ``op["response"]`` or raise :class:`APIError` if the operation failed."""
    if op.get("error"):
        err = op["error"]
        raise APIError(err.get("message", "operation failed"), http_status=200,
                       status=str(err.get("code")), details=err.get("details"))
    return op.get("response")
