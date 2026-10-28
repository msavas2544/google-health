"""Exception hierarchy mapped from Google's standard error envelope."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class GoogleHealthError(Exception):
    """Base class for all errors raised by this library."""


class AuthError(GoogleHealthError):
    """OAuth token acquisition/refresh failed."""


class APIError(GoogleHealthError):
    """The API returned a non-2xx response."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        status: Optional[str] = None,
        details: Optional[List[Dict[str, Any]]] = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(f"{http_status} {status or ''}: {message}".replace("  ", " "))
        self.message = message
        self.http_status = http_status
        self.status = status
        self.details = details or []
        self.response_body = response_body

    @property
    def reason(self) -> Optional[str]:
        """``ErrorInfo.reason`` if the server supplied one (e.g. ``SCOPE_NOT_GRANTED``)."""
        for d in self.details:
            if str(d.get("@type", "")).endswith("google.rpc.ErrorInfo"):
                return d.get("reason")
        return None


class InvalidArgument(APIError):
    pass


class Unauthenticated(APIError):
    pass


class PermissionDenied(APIError):
    pass


class NotFound(APIError):
    pass


class AlreadyExists(APIError):
    pass


class FailedPrecondition(APIError):
    """E.g. webhook endpoint verification failed during subscriber creation."""


class RateLimited(APIError):
    pass


class ServerError(APIError):
    pass


_BY_STATUS = {
    "INVALID_ARGUMENT": InvalidArgument,
    "OUT_OF_RANGE": InvalidArgument,
    "UNAUTHENTICATED": Unauthenticated,
    "PERMISSION_DENIED": PermissionDenied,
    "NOT_FOUND": NotFound,
    "ALREADY_EXISTS": AlreadyExists,
    "FAILED_PRECONDITION": FailedPrecondition,
    "RESOURCE_EXHAUSTED": RateLimited,
}
_BY_HTTP = {400: InvalidArgument, 401: Unauthenticated, 403: PermissionDenied, 404: NotFound,
            409: AlreadyExists, 429: RateLimited}


def from_response(http_status: int, body: Any) -> APIError:
    status = message = None
    details: List[Dict[str, Any]] = []
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        status = err.get("status")
        message = err.get("message")
        details = err.get("details") or []
    if message is None:
        message = body if isinstance(body, str) and body else "request failed"
    cls = _BY_STATUS.get(status or "") or _BY_HTTP.get(http_status)
    if cls is None:
        cls = ServerError if http_status >= 500 else APIError
    return cls(str(message), http_status=http_status, status=status, details=details,
               response_body=body)
