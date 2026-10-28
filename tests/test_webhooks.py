from __future__ import annotations

import base64
import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from google_health.webhooks import (
    SignatureError,
    SignatureVerifier,
    WebhookReceiver,
    is_verification,
    parse_notifications,
)


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _field(num: int, wire: int, payload: bytes) -> bytes:
    key = _varint((num << 3) | wire)
    if wire == 0:
        return key + payload
    return key + _varint(len(payload)) + payload


def make_key(key_id: int, encoding: int = 2, prefix: str = "TINK"):
    priv = ec.generate_private_key(ec.SECP256R1())
    nums = priv.public_key().public_numbers()
    params = _field(1, 0, _varint(3)) + _field(2, 0, _varint(2)) + _field(3, 0, _varint(encoding))
    proto = (_field(1, 0, _varint(0)) + _field(2, 2, params)
             + _field(3, 2, b"\x00" + nums.x.to_bytes(32, "big"))
             + _field(4, 2, b"\x00" + nums.y.to_bytes(32, "big")))
    entry = {
        "keyData": {"typeUrl": "type.googleapis.com/google.crypto.tink.EcdsaPublicKey",
                    "value": base64.b64encode(proto).decode(),
                    "keyMaterialType": "ASYMMETRIC_PUBLIC"},
        "status": "ENABLED", "keyId": key_id, "outputPrefixType": prefix,
    }
    return priv, entry


def sign(priv, key_id: int, body: bytes, encoding: int = 2, prefix: str = "TINK") -> str:
    msg = body + (b"\x00" if prefix == "LEGACY" else b"")
    der = priv.sign(msg, ec.ECDSA(hashes.SHA256()))
    if encoding == 1:
        r, s = decode_dss_signature(der)
        der = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    head = b"" if prefix == "RAW" else (
        (b"\x01" if prefix == "TINK" else b"\x00") + key_id.to_bytes(4, "big"))
    return base64.b64encode(head + der).decode()


BODY = json.dumps([{
    "data": {
        "version": "1",
        "clientProvidedSubscriptionName": "projects/p/subscribers/s/subscriptions/x",
        "healthUserId": "1234567890",
        "operation": "UPSERT",
        "dataType": "steps",
        "intervals": [{
            "physicalTimeInterval": {"startTime": "2026-10-28T10:00:00Z",
                                     "endTime": "2026-10-28T10:15:00Z"},
            "civilDateTimeInterval": {
                "startDateTime": {"date": {"year": 2026, "month": 10, "day": 28},
                                  "time": {"hours": 13}},
                "endDateTime": {"date": {"year": 2026, "month": 10, "day": 28},
                                "time": {"hours": 13, "minutes": 15}}},
            "civilIso8601TimeInterval": {"startTime": "2026-10-28T13:00:00",
                                         "endTime": "2026-10-28T13:15:00"},
        }],
    }
}, {
    "data": {"version": "1", "healthUserId": "1234567890", "operation": "DELETE",
             "dataType": "sleep", "recordId": "abc",
             "intervals": [{"civilIso8601TimeInterval": {"startTime": "2026-10-27T23:00:00",
                                                         "endTime": "2026-10-28T07:00:00"}}]}
}]).encode()


def test_parse_notifications() -> None:
    n1, n2 = parse_notifications(BODY)
    assert n1.data_type == "steps" and n1.operation == "UPSERT" and not n1.is_delete
    iv = n1.intervals[0]
    assert iv.start == dt.datetime(2026, 10, 28, 10, tzinfo=dt.timezone.utc)
    assert iv.civil_start == dt.datetime(2026, 10, 28, 13)
    assert iv.civil_end == dt.datetime(2026, 10, 28, 13, 15)
    assert n2.is_delete and n2.record_id == "abc"
    assert n2.intervals[0].start is None and n2.intervals[0].civil_end == dt.datetime(
        2026, 10, 28, 7)
    single = parse_notifications(json.dumps(json.loads(BODY)[0]))
    assert len(single) == 1


@pytest.mark.parametrize("encoding,prefix", [(2, "TINK"), (1, "TINK"), (2, "LEGACY"),
                                             (2, "RAW")])
def test_signature_roundtrip(encoding: int, prefix: str) -> None:
    priv, entry = make_key(2280854714, encoding, prefix)
    _, other = make_key(99)
    v = SignatureVerifier(keyset={"primaryKeyId": 2280854714, "key": [other, entry]})
    sig = sign(priv, 2280854714, BODY, encoding, prefix)
    v.verify(BODY, sig)
    assert not v.is_valid(BODY + b" ", sig)


def test_unknown_key_triggers_refetch() -> None:
    old_priv, old = make_key(1)
    new_priv, new = make_key(2)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return {"key": [old]} if len(fetched) == 1 else {"key": [old, new]}

    v = SignatureVerifier(fetch=fetch)
    v.verify(BODY, sign(old_priv, 1, BODY))
    assert len(fetched) == 1
    v._fetched_at -= 120  # allow a forced refresh
    v.verify(BODY, sign(new_priv, 2, BODY))
    assert len(fetched) == 2
    with pytest.raises(SignatureError):
        v.verify(BODY, "!!!")


def test_receiver_flow() -> None:
    priv, entry = make_key(7)
    rx = WebhookReceiver(["Bearer old", "Bearer new"],
                         verifier=SignatureVerifier(keyset={"key": [entry]}))
    probe = b'{"type": "verification"}'
    assert is_verification(probe)
    assert rx.handle({"authorization": "Bearer new"}, probe).status_code == 201
    assert rx.handle({}, probe).status_code == 401
    assert rx.handle({"Authorization": "Bearer wrong"}, BODY).status_code == 401

    sig = sign(priv, 7, BODY)
    ok = rx.handle({"Authorization": "Bearer old", "Google-Health-Api-Signature": sig}, BODY)
    assert ok.status_code == 204 and len(ok.notifications) == 2
    bad = rx.handle({"Authorization": "Bearer old", "GOOGLE-HEALTH-API-SIGNATURE": sig},
                    BODY.replace(b"steps", b"floor"))
    assert bad.status_code == 403
    missing = rx.handle({"Authorization": "Bearer old"}, BODY)
    assert missing.status_code == 403

    plain = WebhookReceiver("s3cret", verify_signatures=False)
    assert plain.handle({"Authorization": "s3cret"}, BODY).status_code == 204
    assert plain.handle({"Authorization": "Bearer s3cret"}, BODY).status_code == 204
