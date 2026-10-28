"""asyncio client for the Google Health API v4 (same surface as :class:`HealthClient`)."""

from __future__ import annotations

import asyncio
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Union,
)

import httpx

from . import _api
from ._api import Call, DataTypeLike, FamilyLike, PagedCall
from ._time import DateLike, DateTimeLike, DurationLike
from .auth import Credentials
from .client import _backoff, _Base, _decode, _error
from .summaries import DailyValue, WindowValue, daily_value, window_value


class AsyncHealthClient(_Base):
    """Non-blocking client.

    >>> async with AsyncHealthClient(creds) as client:
    ...     async for p in client.data_points.list("weight", "2026-01-01", "2026-02-01"):
    ...         print(p["weight"]["weightGrams"])
    """

    def __init__(
        self,
        credentials: Union[Credentials, str],
        *,
        http: Optional[httpx.AsyncClient] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        **kwargs: Any,
    ) -> None:
        super().__init__(credentials, **kwargs)
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(timeout=self.timeout)
        self._sleep = sleep
        self.users = AsyncUsers(self)
        self.data_points = AsyncDataPoints(self)
        self.devices = AsyncDevices(self)
        self.subscribers = AsyncSubscribers(self)

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()

    async def __aenter__(self) -> AsyncHealthClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def request(self, call: Call) -> Any:
        attempt = 0
        reauthed = False
        while True:
            token = await self.credentials.atoken(self.http)
            resp = await self.http.request(
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
                await self._sleep(_backoff(attempt, self.backoff, resp.headers.get("Retry-After")))
                attempt += 1
                continue
            raise _error(resp)

    async def paginate(self, paged: PagedCall) -> AsyncIterator[Dict[str, Any]]:
        call = paged.call
        seen = 0
        while True:
            page = await self.request(call)
            for item in page.get(paged.items_key) or []:
                yield item
                seen += 1
                if paged.limit is not None and seen >= paged.limit:
                    return
            token = page.get("nextPageToken")
            if not token:
                return
            call = paged.with_token(token)

    async def _paginate_all(self, pages: Iterable[PagedCall]) -> AsyncIterator[Dict[str, Any]]:
        for p in pages:
            async for item in self.paginate(p):
                yield item

    async def _collect(self, it: AsyncIterator[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [x async for x in it]

    async def identity(self) -> Dict[str, Any]:
        return await self.users.get_identity()

    async def daily(
        self, data_type: DataTypeLike, start: DateLike, end: DateLike, *,
        window_days: int = 1, data_source_family: FamilyLike = None,
    ) -> List[DailyValue]:
        pts = await self._collect(self.data_points.daily_roll_up(
            data_type, start, end, window_days=window_days,
            data_source_family=data_source_family))
        return sorted((daily_value(data_type, p) for p in pts), key=lambda d: d.date)

    async def windows(
        self, data_type: DataTypeLike, start: DateTimeLike, end: DateTimeLike,
        window: DurationLike, *, data_source_family: FamilyLike = None,
    ) -> List[WindowValue]:
        pts = await self._collect(self.data_points.roll_up(
            data_type, start, end, window, data_source_family=data_source_family))
        return sorted((window_value(data_type, p) for p in pts), key=lambda w: w.start)


class _NS:
    def __init__(self, client: AsyncHealthClient) -> None:
        self._c = client

    def _user(self, user: Optional[str]) -> str:
        return user or self._c.user


class AsyncUsers(_NS):
    async def get_identity(self, user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.get_identity(self._user(user)))

    async def get_profile(self, user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.get_profile(self._user(user)))

    async def update_profile(self, profile: Dict[str, Any], update_mask: Any = None,
                             user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.update_profile(self._user(user), profile, update_mask))

    async def get_settings(self, user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.get_settings(self._user(user)))

    async def update_settings(self, settings: Dict[str, Any], update_mask: Any = None,
                              user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(
            _api.update_settings(self._user(user), settings, update_mask))

    async def get_irn_profile(self, user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.get_irn_profile(self._user(user)))


class AsyncDevices(_NS):
    async def list(self, *, page_size: Optional[int] = None,
                   user: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self._c._collect(
            self._c.paginate(_api.list_devices(self._user(user), page_size)))

    async def get(self, device_id: str, user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.get_device(self._user(user), device_id))


class AsyncDataPoints(_NS):
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
    ) -> AsyncIterator[Dict[str, Any]]:
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
    ) -> AsyncIterator[Dict[str, Any]]:
        return self._c.paginate(_api.list_data_points(
            self._user(user), data_type, start, end, civil=civil, filter=filter,
            family=data_source_family, page_size=page_size, limit=limit, reconcile=True))

    async def get(self, data_type: DataTypeLike, point_id: str,
                  user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.get_data_point(self._user(user), data_type, point_id))

    async def create(self, data_type: DataTypeLike, data_point: Dict[str, Any],
                     user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(
            _api.create_data_point(self._user(user), data_type, data_point))

    async def patch(self, data_type: DataTypeLike, point_id: str, data_point: Dict[str, Any],
                    user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(
            _api.patch_data_point(self._user(user), data_type, point_id, data_point))

    async def batch_delete(self, data_type: DataTypeLike, point_ids: Sequence[str],
                           user: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.batch_delete(self._user(user), data_type, point_ids))

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
    ) -> AsyncIterator[Dict[str, Any]]:
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
    ) -> AsyncIterator[Dict[str, Any]]:
        return self._c._paginate_all(_api.daily_roll_up(
            self._user(user), data_type, start, end, window_days,
            family=data_source_family, page_size=page_size))

    async def export_exercise_tcx(self, exercise_id: str, *, partial_data: bool = False,
                                  user: Optional[str] = None) -> bytes:
        return await self._c.request(
            _api.export_exercise_tcx(self._user(user), exercise_id, partial_data))


class AsyncSubscribers(_NS):
    async def create(
        self,
        endpoint_uri: str,
        secret: str,
        data_types: Sequence[DataTypeLike],
        *,
        policy: str = "AUTOMATIC",
        subscriber_id: Optional[str] = None,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._c.request(_api.create_subscriber(
            self._c._project(project), endpoint_uri, secret, data_types, policy, subscriber_id))

    async def list(self, *, page_size: Optional[int] = None,
                   project: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self._c._collect(self._c.paginate(
            _api.list_subscribers(self._c._project(project), page_size)))

    async def patch(self, subscriber: str, body: Dict[str, Any], update_mask: Any = None,
                    project: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(_api.patch_subscriber(
            self._c._project(project), subscriber, body, update_mask))

    async def rotate_secret(self, subscriber: str, new_secret: str,
                            project: Optional[str] = None) -> Dict[str, Any]:
        return await self.patch(subscriber, {"endpointAuthorization": {"secret": new_secret}},
                                "endpointAuthorization", project=project)

    async def delete(self, subscriber: str, *, force: bool = False,
                     project: Optional[str] = None) -> Dict[str, Any]:
        return await self._c.request(
            _api.delete_subscriber(self._c._project(project), subscriber, force))

    async def subscribe_user(
        self, subscriber: str, user: str, data_types: Sequence[DataTypeLike], *,
        subscription_id: Optional[str] = None, project: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._c.request(_api.create_subscription(
            self._c._project(project), subscriber, user, data_types, subscription_id))

    async def list_subscriptions(
        self, subscriber: str, *, filter: Optional[str] = None,
        page_size: Optional[int] = None, project: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return await self._c._collect(self._c.paginate(_api.list_subscriptions(
            self._c._project(project), subscriber, filter, page_size)))

    async def update_subscription(self, name: str, user: str,
                                  data_types: Sequence[DataTypeLike]) -> Dict[str, Any]:
        return await self._c.request(_api.patch_subscription(name, user, data_types))

    async def unsubscribe(self, name: str) -> Dict[str, Any]:
        return await self._c.request(_api.delete_subscription(name))
