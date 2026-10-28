from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

import httpx
import pytest

from google_health import AsyncHealthClient, HealthClient, StaticToken

Handler = Callable[[httpx.Request], httpx.Response]


class FakeAPI:
    """Records requests and dispatches them to handlers keyed by (method, path-suffix)."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.routes: List[tuple] = []

    def add(self, method: str, path_contains: str, handler: Any) -> None:
        self.routes.append((method, path_contains, handler))

    def json_body(self, i: int = -1) -> Dict[str, Any]:
        return json.loads(self.requests[i].content or b"{}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, frag, handler in self.routes:
            if request.method == method and frag in request.url.path:
                if callable(handler):
                    return handler(request)
                if isinstance(handler, list):
                    return handler.pop(0) if len(handler) > 1 else handler[0]
                return httpx.Response(200, json=handler)
        return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND",
                                                   "message": f"no route {request.url}"}})


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def client(api: FakeAPI) -> HealthClient:
    http = httpx.Client(transport=httpx.MockTransport(api))
    return HealthClient(StaticToken("tok"), http=http, sleep=lambda s: None)


@pytest.fixture
def aclient(api: FakeAPI) -> AsyncHealthClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(api))

    async def nosleep(_: float) -> None:
        return None

    return AsyncHealthClient(StaticToken("tok"), http=http, sleep=nosleep)
