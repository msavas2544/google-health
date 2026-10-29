"""Python client for the Google Health API (the successor of the Fitbit Web API)."""

from ._api import operation_result
from ._version import __version__
from .async_client import AsyncHealthClient
from .auth import (
    Credentials,
    GoogleAuthCredentials,
    OAuthCredentials,
    OAuthFlow,
    StaticToken,
    revoke,
)
from .client import HealthClient
from .errors import (
    AccountNotLinked,
    AlreadyExists,
    APIError,
    AuthError,
    FailedPrecondition,
    GoogleHealthError,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    RateLimited,
    ServerError,
    Unauthenticated,
)
from .filters import time_range_filter
from .summaries import DailyValue, WindowValue, rollup_value
from .types import DataKind, DataSourceFamily, DataType, Scope

__all__ = [
    "__version__",
    "HealthClient",
    "AsyncHealthClient",
    "Credentials",
    "OAuthCredentials",
    "OAuthFlow",
    "StaticToken",
    "GoogleAuthCredentials",
    "revoke",
    "DataType",
    "DataKind",
    "DataSourceFamily",
    "Scope",
    "DailyValue",
    "WindowValue",
    "rollup_value",
    "time_range_filter",
    "operation_result",
    "GoogleHealthError",
    "AuthError",
    "APIError",
    "InvalidArgument",
    "Unauthenticated",
    "PermissionDenied",
    "NotFound",
    "AlreadyExists",
    "AccountNotLinked",
    "FailedPrecondition",
    "RateLimited",
    "ServerError",
]
