"""Receive and verify Google Health API webhook notifications.

Typical use inside any web framework::

    receiver = WebhookReceiver(secret=os.environ["HEALTH_WEBHOOK_SECRET"])

    @app.post("/health-webhook")
    def hook():
        result = receiver.handle(request.headers, request.get_data())
        for n in result.notifications:
            queue.enqueue(sync_user, n.health_user_id, n.data_type, n.intervals)
        return "", result.status_code

The receiver implements Google's endpoint verification handshake (accept the
authorized probe, reject the unauthenticated one), checks the shared secret,
and verifies the ``GOOGLE-HEALTH-API-SIGNATURE`` ECDSA signature against
Google's published Tink keyset. Signature verification needs the
``cryptography`` package (``pip install google-health[webhooks]``).
"""

from __future__ import annotations

import base64
import datetime as dt
import hmac
import json
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ._time import parse_civil_datetime, parse_timestamp
from .errors import GoogleHealthError

KEYSET_URL = "https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json"
SIGNATURE_HEADER = "GOOGLE-HEALTH-API-SIGNATURE"
USER_AGENT = "Google-Health-API-Webhooks"


class SignatureError(GoogleHealthError):
    pass


# ----------------------------------------------------------------------------- models
@dataclass(frozen=True)
class NotificationInterval:
    #: UTC instants (absent for daily/full-day data).
    start: Optional[dt.datetime]
    end: Optional[dt.datetime]
    #: Local wall-clock bounds (naive datetimes).
    civil_start: Optional[dt.datetime]
    civil_end: Optional[dt.datetime]


@dataclass(frozen=True)
class Notification:
    health_user_id: str
    data_type: str
    operation: str
    intervals: Tuple[NotificationInterval, ...]
    subscription_name: Optional[str] = None
    record_id: Optional[str] = None
    version: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_delete(self) -> bool:
        return self.operation == "DELETE"


def _iso_local(s: Optional[str]) -> Optional[dt.datetime]:
    return dt.datetime.fromisoformat(s) if s else None


def _interval(raw: Mapping[str, Any]) -> NotificationInterval:
    phys = raw.get("physicalTimeInterval") or {}
    civ = raw.get("civilDateTimeInterval") or {}
    iso = raw.get("civilIso8601TimeInterval") or {}
    civil_start = parse_civil_datetime(civ.get("startDateTime")) or _iso_local(iso.get("startTime"))
    civil_end = parse_civil_datetime(civ.get("endDateTime")) or _iso_local(iso.get("endTime"))
    return NotificationInterval(
        start=parse_timestamp(phys.get("startTime")),
        end=parse_timestamp(phys.get("endTime")),
        civil_start=civil_start,
        civil_end=civil_end,
    )


def _load(body: Union[bytes, str, Mapping[str, Any], Sequence[Any]]) -> Any:
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    if isinstance(body, str):
        return json.loads(body) if body.strip() else {}
    return body


def is_verification(body: Union[bytes, str, Mapping[str, Any], Sequence[Any]]) -> bool:
    """True for the ``{"type": "verification"}`` probe sent while creating a subscriber."""
    try:
        data = _load(body)
    except ValueError:
        return False
    return isinstance(data, Mapping) and data.get("type") == "verification"


def parse_notifications(
    body: Union[bytes, str, Mapping[str, Any], Sequence[Any]],
) -> List[Notification]:
    """Parse a single notification object or a batch (JSON array, ≤ 99 items)."""
    data = _load(body)
    items = data if isinstance(data, list) else [data]
    out: List[Notification] = []
    for item in items:
        d = item.get("data", item) if isinstance(item, Mapping) else None
        if not isinstance(d, Mapping) or "dataType" not in d:
            continue
        out.append(Notification(
            health_user_id=str(d.get("healthUserId", "")),
            data_type=str(d["dataType"]),
            operation=str(d.get("operation", "UPSERT")),
            intervals=tuple(_interval(i) for i in d.get("intervals") or []),
            subscription_name=d.get("clientProvidedSubscriptionName"),
            record_id=d.get("recordId"),
            version=d.get("version"),
            raw=d,
        ))
    return out


# ----------------------------------------------------------------------------- Tink keyset
def _varint(buf: bytes, i: int) -> Tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise SignatureError("truncated protobuf")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _proto_fields(buf: bytes) -> Dict[int, List[Union[int, bytes]]]:
    out: Dict[int, List[Union[int, bytes]]] = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wt = key >> 3, key & 7
        val: Union[int, bytes]
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt == 2:
            ln, i = _varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wt == 5:
            val, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        elif wt == 1:
            val, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        else:
            raise SignatureError(f"unsupported protobuf wire type {wt}")
        out.setdefault(num, []).append(val)
    return out


# Tink enums (proto/common.proto)
_HASHES = {1: "SHA1", 2: "SHA384", 3: "SHA256", 4: "SHA512", 5: "SHA224"}
_CURVES = {2: "SECP256R1", 3: "SECP384R1", 4: "SECP521R1"}
_ENC_P1363, _ENC_DER = 1, 2


@dataclass
class _Key:
    key_id: int
    prefix: str  # TINK | LEGACY | RAW
    public_key: Any
    hash_name: str
    encoding: int


def _parse_key(entry: Mapping[str, Any]) -> Optional[_Key]:
    from cryptography.hazmat.primitives.asymmetric import ec

    if entry.get("status", "ENABLED") != "ENABLED":
        return None
    kd = entry.get("keyData") or {}
    if "EcdsaPublicKey" not in str(kd.get("typeUrl", "EcdsaPublicKey")):
        return None
    f = _proto_fields(base64.b64decode(kd["value"]))
    params = _proto_fields(f[2][0]) if 2 in f else {}  # type: ignore[arg-type]
    hash_name = _HASHES.get(int(params.get(1, [3])[0]), "SHA256")  # type: ignore[arg-type]
    curve_name = _CURVES.get(int(params.get(2, [2])[0]), "SECP256R1")  # type: ignore[arg-type]
    encoding = int(params.get(3, [_ENC_DER])[0])  # type: ignore[arg-type]
    x = int.from_bytes(f[3][0], "big")  # type: ignore[arg-type]
    y = int.from_bytes(f[4][0], "big")  # type: ignore[arg-type]
    curve = getattr(ec, curve_name)()
    pub = ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
    return _Key(int(entry.get("keyId", 0)), str(entry.get("outputPrefixType", "TINK")),
                pub, hash_name, encoding)


def parse_keyset(keyset: Mapping[str, Any]) -> List[Any]:
    """Parse a Tink JSON public keyset into verification keys."""
    entries = keyset.get("key") or keyset.get("keys") or []
    keys = [k for k in (_parse_key(e) for e in entries) if k is not None]
    if not keys:
        raise SignatureError("keyset contains no usable ECDSA public keys")
    return keys


def _default_fetch(url: str) -> Mapping[str, Any]:
    import httpx

    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


class SignatureVerifier:
    """Verifies ``GOOGLE-HEALTH-API-SIGNATURE`` headers. Thread-safe, caches the keyset
    for ``ttl`` seconds and refetches when it sees an unknown key id (keys rotate
    every 30 days)."""

    def __init__(
        self,
        keyset_url: str = KEYSET_URL,
        *,
        fetch: Callable[[str], Mapping[str, Any]] = _default_fetch,
        ttl: float = 3600,
        keyset: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.keyset_url = keyset_url
        self._fetch = fetch
        self.ttl = ttl
        self._lock = threading.Lock()
        self._keys: List[_Key] = parse_keyset(keyset) if keyset is not None else []
        self._fetched_at = time.time() if keyset is not None else 0.0
        self._static = keyset is not None

    def _refresh(self, force: bool = False) -> None:
        if self._static:
            return
        with self._lock:
            age = time.time() - self._fetched_at
            if self._keys and not force and age < self.ttl:
                return
            if force and age < 60:  # don't hammer gstatic on bad signatures
                return
            try:
                self._keys = parse_keyset(self._fetch(self.keyset_url))
            except Exception as exc:
                # Keep serving with cached keys if gstatic is briefly unreachable.
                self._fetched_at = time.time() - self.ttl + 60  # retry in a minute
                if not self._keys:
                    raise SignatureError(f"could not fetch Google keyset: {exc}") from exc
                return
            self._fetched_at = time.time()

    def _candidates(self, sig: bytes) -> List[Tuple[_Key, bytes, bytes]]:
        out = []
        for k in self._keys:
            if k.prefix in ("TINK", "LEGACY") and len(sig) > 5 and 0 <= k.key_id < 2**32:
                version = b"\x01" if k.prefix == "TINK" else b"\x00"
                expected = version + k.key_id.to_bytes(4, "big")
                if sig[:5] == expected:
                    out.append((k, sig[5:], b"\x00" if k.prefix == "LEGACY" else b""))
            elif k.prefix == "RAW":
                out.append((k, sig, b""))
        return out

    def verify(self, body: bytes, signature_header: str) -> None:
        """Raise :class:`SignatureError` unless the signature is valid for ``body``."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        if not signature_header:
            raise SignatureError("missing signature header")
        try:
            sig = base64.b64decode(signature_header.strip(), validate=False)
        except (ValueError, TypeError) as exc:
            raise SignatureError("signature is not valid base64") from exc

        self._refresh()
        cands = self._candidates(sig)
        if not cands:
            self._refresh(force=True)
            cands = self._candidates(sig)
        if not cands:
            raise SignatureError("signature key id not found in Google keyset")
        for key, raw_sig, suffix in cands:
            if key.encoding == _ENC_P1363:
                half = len(raw_sig) // 2
                raw_sig = encode_dss_signature(int.from_bytes(raw_sig[:half], "big"),
                                               int.from_bytes(raw_sig[half:], "big"))
            try:
                key.public_key.verify(raw_sig, body + suffix,
                                      ec.ECDSA(getattr(hashes, key.hash_name)()))
                return
            except InvalidSignature:
                continue
        raise SignatureError("invalid signature")

    def is_valid(self, body: bytes, signature_header: str) -> bool:
        try:
            self.verify(body, signature_header)
            return True
        except SignatureError:
            return False


# ----------------------------------------------------------------------------- receiver
@dataclass
class WebhookResult:
    #: HTTP status your endpoint should return (204 for accepted notifications).
    status_code: int
    notifications: List[Notification] = field(default_factory=list)
    verification: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status_code < 400


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    get = getattr(headers, "get", None)
    if get is not None:
        v = get(name)
        if v is not None:
            return str(v)
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return str(v)
    return None


class WebhookReceiver:
    """Framework-agnostic webhook endpoint logic.

    :param secret: the ``endpointAuthorization.secret`` configured on the subscriber.
        Pass a list during secret rotation to accept old and new values.
    :param verify_signatures: also check Google's ECDSA signature (recommended).
    """

    def __init__(
        self,
        secret: Union[str, Sequence[str]],
        *,
        verify_signatures: bool = True,
        verifier: Optional[SignatureVerifier] = None,
    ) -> None:
        self.secrets = [secret] if isinstance(secret, str) else list(secret)
        if not self.secrets or not all(self.secrets):
            raise ValueError("at least one non-empty secret is required")
        self.verify_signatures = verify_signatures
        self.verifier = verifier or (SignatureVerifier() if verify_signatures else None)

    def authorized(self, authorization: Optional[str]) -> bool:
        if not authorization:
            return False
        got = authorization.strip().encode()
        ok = False
        for s in self.secrets:
            for candidate in (s, f"Bearer {s}"):
                ok |= hmac.compare_digest(got, candidate.encode())
        return ok

    def handle(self, headers: Mapping[str, str], body: bytes) -> WebhookResult:
        if not self.authorized(_header(headers, "Authorization")):
            return WebhookResult(401, error="unauthorized")
        if is_verification(body):
            # The subscribers.create spec expects "201 Created" for the authorized probe.
            return WebhookResult(201, verification=True)
        if self.verify_signatures and self.verifier is not None:
            try:
                self.verifier.verify(body, _header(headers, SIGNATURE_HEADER) or "")
            except SignatureError as exc:
                return WebhookResult(403, error=str(exc))
        try:
            notes = parse_notifications(body)
        except ValueError as exc:
            return WebhookResult(400, error=f"invalid JSON: {exc}")
        return WebhookResult(204, notifications=notes)
