"""Synchronous client for the Google Health API v4."""

from __future__ import annotations

import random
import time
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Union,
)

import httpx

from . import _api
from ._api import Call, DataTypeLike, FamilyLike, PagedCall
from ._time import DateLike, DateTimeLike, DurationLike
from .auth import Credentials, StaticToken
from .errors import from_response
from .summaries import DailyValue, WindowValue, daily_value, window_value


def _backoff(attempt: int, base: float, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return min(base * (2 ** attempt), 30.0) * (0.5 + random.random() / 2)


def _decode(resp: httpx.Response, raw: bool) -> Any:
    if raw:
        return resp.content
    if not resp.content:
        return {}
    return resp.json()


def _error(resp: httpx.Response) -> Exception:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    return from_response(resp.status_code, body)


class _Base:
    def __init__(
        self,
        credentials: Union[Credentials, str],
        *,
        user: str = "me",
        project: Optional[str] = None,
        base_url: str = _api.DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 0.5,
    ) -> None:
        self.credentials = StaticToken(credentials) if isinstance(credentials, str) else credentials
        self.user = user
        self.project = project
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff

    def _project(self, project: Optional[str]) -> str:
        p = project or self.project
        if not p:
            raise ValueError("a Google Cloud project is required (pass project=...)")
        return p

    def _should_retry(self, call: Call, status: int, attempt: int) -> bool:
        if attempt >= self.max_retries or status not in _api.RETRY_STATUSES:
            return False
        return call.idempotent or status in (429, 503)


class HealthClient(_Base):
    """Blocking client.

    >>> from google_health import HealthClient, OAuthCredentials
    >>> client = HealthClient(OAuthCredentials.load("token.json"))
    >>> for day in client.daily("steps", "2026-10-01", "2026-10-08"):
    ...     print(day.date, day.value)
    """

    def __init__(
        self,
        credentials: Union[Credentials, str],
        *,
        http: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
        **kwargs: Any,
    ) -> None:
        super().__init__(credentials, **kwargs)
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=self.timeout)
        self._sleep = sleep
        self.users = Users(self)
        self.data_points = DataPoints(self)
        self.devices = Devices(self)
        self.subscribers = Subscribers(self)

    # -- lifecycle ---------------------------------------------------------------
    def close(self) -> None:
        if self._own_http:
            self.http.close()

    def __enter__(self) -> HealthClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport ---------------------------------------------------------------
    def request(self, call: Call) -> Any:
        attempt = 0
        reauthed = False
        while True:
            token = self.credentials.token(self.http)
            resp = self.http.request(
                call.method,
                self.base_url + call.path,
                params=call.params,
                json=call.json,
                headers={"Authorization": f"Bearer {token}", "User-Agent": _api.USER_AGENT},
            )
            if resp.status_code == 401 and not reauthed:
                reauthed = True
                self.credentials.invalidate()
                continue
            if resp.status_code < 400:
                return _decode(resp, call.raw)
            if self._should_retry(call, resp.status_code, attempt):
                self._sleep(_backoff(attempt, self.backoff, resp.headers.get("Retry-After")))
                attempt += 1
                continue
            raise _error(resp)

    def paginate(self, paged: PagedCall) -> Iterator[Dict[str, Any]]:
        call = paged.call
        seen = 0
        while True:
            page = self.request(call)
            for item in page.get(paged.items_key) or []:
                yield item
                seen += 1
                if paged.limit is not None and seen >= paged.limit:
                    return
            token = page.get("nextPageToken")
            if not token:
                return
            call = paged.with_token(token)

    def _paginate_all(self, pages: Iterable[PagedCall]) -> Iterator[Dict[str, Any]]:
        for p in pages:
            yield from self.paginate(p)

    # -- convenience -------------------------------------------------------------
    def identity(self) -> Dict[str, Any]:
        """``{"healthUserId", "legacyUserId" (Fitbit), "name"}`` for the current user."""
        return self.users.get_identity()

    def daily(
        self, data_type: DataTypeLike, start: DateLike, end: DateLike, *,
        window_days: int = 1, data_source_family: FamilyLike = None,
    ) -> List[DailyValue]:
        """Per-day totals/averages for ``[start, end)`` in the user's local days.

        Ranges longer than the API limit (14 or 90 days) are split automatically.
        """
        pts = self.data_points.daily_roll_up(
            data_type, start, end, window_days=window_days,
            data_source_family=data_source_family)
        return sorted((daily_value(data_type, p) for p in pts), key=lambda d: d.date)

    def windows(
        self, data_type: DataTypeLike, start: DateTimeLike, end: DateTimeLike,
        window: DurationLike, *, data_source_family: FamilyLike = None,
    ) -> List[WindowValue]:
        """Values aggregated over fixed physical-time windows (e.g. 15 minutes)."""
        pts = self.data_points.roll_up(data_type, start, end, window,
                                       data_source_family=data_source_family)
        return sorted((window_value(data_type, p) for p in pts), key=lambda w: w.start)


class _NS:
    def __init__(self, client: HealthClient) -> None:
        self._c = client

    def _user(self, user: Optional[str]) -> str:
        return user or self._c.user


class Users(_NS):
    def get_identity(self, user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.get_identity(self._user(user)))

    def get_profile(self, user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.get_profile(self._user(user)))

    def update_profile(self, profile: Dict[str, Any], update_mask: Any = None,
                       user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.update_profile(self._user(user), profile, update_mask))

    def get_settings(self, user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.get_settings(self._user(user)))

    def update_settings(self, settings: Dict[str, Any], update_mask: Any = None,
                        user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.update_settings(self._user(user), settings, update_mask))

    def get_irn_profile(self, user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.get_irn_profile(self._user(user)))


class Devices(_NS):
    def list(self, *, page_size: Optional[int] = None,
             user: Optional[str] = None) -> List[Dict[str, Any]]:
        return list(self._c.paginate(_api.list_devices(self._user(user), page_size)))

    def get(self, device_id: str, user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.get_device(self._user(user), device_id))


class DataPoints(_NS):
    def list(
        self,
        data_type: DataTypeLike,
        start: Optional[DateTimeLike] = None,
        end: Optional[DateTimeLike] = None,
        *,
        civil: bool = False,
        filter: Optional[str] = None,
        data_source_family: FamilyLike = None,
        page_size: Optional[int] = None,
        limit: Optional[int] = None,
        user: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Iterate raw data points (newest first), following pagination lazily.

        ``start``/``end`` build the right filter for the data type automatically;
        pass ``filter`` to supply your own AIP-160 expression instead.
        """
        return self._c.paginate(_api.list_data_points(
            self._user(user), data_type, start, end, civil=civil, filter=filter,
            family=data_source_family, page_size=page_size, limit=limit))

    def reconcile(
        self,
        data_type: DataTypeLike,
        start: Optional[DateTimeLike] = None,
        end: Optional[DateTimeLike] = None,
        *,
        civil: bool = False,
        filter: Optional[str] = None,
        data_source_family: FamilyLike = None,
        page_size: Optional[int] = None,
        limit: Optional[int] = None,
        user: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Like :meth:`list` but merged into a single de-duplicated stream."""
        return self._c.paginate(_api.list_data_points(
            self._user(user), data_type, start, end, civil=civil, filter=filter,
            family=data_source_family, page_size=page_size, limit=limit, reconcile=True))

    def get(self, data_type: DataTypeLike, point_id: str,
            user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.get_data_point(self._user(user), data_type, point_id))

    def create(self, data_type: DataTypeLike, data_point: Dict[str, Any],
               user: Optional[str] = None) -> Dict[str, Any]:
        """Create a data point. ``data_point`` may be the full DataPoint or only the
        type payload (e.g. ``{"weightGrams": 72500, "sampleTime": {...}}``).
        Returns the long-running Operation."""
        return self._c.request(_api.create_data_point(self._user(user), data_type, data_point))

    def patch(self, data_type: DataTypeLike, point_id: str, data_point: Dict[str, Any],
              user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(
            _api.patch_data_point(self._user(user), data_type, point_id, data_point))

    def batch_delete(self, data_type: DataTypeLike, point_ids: Sequence[str],
                     user: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.batch_delete(self._user(user), data_type, point_ids))

    def roll_up(
        self,
        data_type: DataTypeLike,
        start: DateTimeLike,
        end: DateTimeLike,
        window: DurationLike,
        *,
        data_source_family: FamilyLike = None,
        page_size: Optional[int] = None,
        user: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Aggregate over physical-time windows. Long ranges are chunked for you."""
        return self._c._paginate_all(_api.roll_up(
            self._user(user), data_type, start, end, window,
            family=data_source_family, page_size=page_size))

    def daily_roll_up(
        self,
        data_type: DataTypeLike,
        start: DateLike,
        end: DateLike,
        *,
        window_days: int = 1,
        data_source_family: FamilyLike = None,
        page_size: Optional[int] = None,
        user: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Aggregate over civil (local) days in ``[start, end)``; chunked automatically."""
        return self._c._paginate_all(_api.daily_roll_up(
            self._user(user), data_type, start, end, window_days,
            family=data_source_family, page_size=page_size))

    def export_exercise_tcx(self, exercise_id: str, *, partial_data: bool = False,
                            user: Optional[str] = None) -> bytes:
        """Download an exercise as a TCX (XML) document."""
        return self._c.request(
            _api.export_exercise_tcx(self._user(user), exercise_id, partial_data))


class Subscribers(_NS):
    """Webhook subscriber management (project-level; use service-account credentials)."""

    def create(
        self,
        endpoint_uri: str,
        secret: str,
        data_types: Sequence[DataTypeLike],
        *,
        policy: str = "AUTOMATIC",
        subscriber_id: Optional[str] = None,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register an HTTPS endpoint. Google verifies it synchronously: your endpoint
        must accept a POST carrying ``Authorization: <secret>`` and reject one without
        it – :class:`google_health.webhooks.WebhookReceiver` does that for you."""
        return self._c.request(_api.create_subscriber(
            self._c._project(project), endpoint_uri, secret, data_types, policy, subscriber_id))

    def list(self, *, page_size: Optional[int] = None,
             project: Optional[str] = None) -> List[Dict[str, Any]]:
        return list(self._c.paginate(
            _api.list_subscribers(self._c._project(project), page_size)))

    def patch(self, subscriber: str, body: Dict[str, Any], update_mask: Any = None,
              project: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(_api.patch_subscriber(
            self._c._project(project), subscriber, body, update_mask))

    def rotate_secret(self, subscriber: str, new_secret: str,
                      project: Optional[str] = None) -> Dict[str, Any]:
        return self.patch(subscriber, {"endpointAuthorization": {"secret": new_secret}},
                          "endpointAuthorization", project=project)

    def delete(self, subscriber: str, *, force: bool = False,
               project: Optional[str] = None) -> Dict[str, Any]:
        return self._c.request(
            _api.delete_subscriber(self._c._project(project), subscriber, force))

    # -- per-user subscriptions (MANUAL policy) ----------------------------------------
    def subscribe_user(
        self, subscriber: str, user: str, data_types: Sequence[DataTypeLike], *,
        subscription_id: Optional[str] = None, project: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._c.request(_api.create_subscription(
            self._c._project(project), subscriber, user, data_types, subscription_id))

    def list_subscriptions(
        self, subscriber: str, *, filter: Optional[str] = None,
        page_size: Optional[int] = None, project: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return list(self._c.paginate(_api.list_subscriptions(
            self._c._project(project), subscriber, filter, page_size)))

    def update_subscription(self, name: str, user: str,
                            data_types: Sequence[DataTypeLike]) -> Dict[str, Any]:
        return self._c.request(_api.patch_subscription(name, user, data_types))

    def unsubscribe(self, name: str) -> Dict[str, Any]:
        return self._c.request(_api.delete_subscription(name))
