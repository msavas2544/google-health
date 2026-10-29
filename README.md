# google-health

A Python client for the **Google Health API v4**, which replaces the Fitbit Web API. It also includes a migration layer for `python-fitbit` users.

> The legacy Fitbit Web API and its OAuth endpoints are scheduled to be **turned off on 2 November 2026**. After that date, apps still using it will stop receiving Fitbit / Pixel Watch data.

`google-health` gives you:

* **Sync and async clients** that share one API surface (`HealthClient`, `AsyncHealthClient`) and run on Python 3.9+.
* **Smart filters**: pass `start`/`end` and the library builds the right AIP-160 filter for each data type. You never need to remember whether it's `steps.interval.start_time` or `weight.sample_time.physical_time`.
* **Roll-ups without limits**: ranges longer than the API's 14- or 90-day caps are split for you and pages are followed automatically. `client.daily("steps", ...)` returns plain `(date, value)` rows in metres, kg, kcal and bpm.
* **OAuth 2.0** with PKCE, automatic token refresh, refresh-token rotation support and file persistence. A one-command `google-health login` handles local scripts.
* **Webhooks**: one class handles the endpoint-verification handshake, checks the shared secret, verifies Google's ECDSA signature against the published Tink keyset (with key rotation) and parses batched notifications.
* **A `python-fitbit` compatibility layer**: change one import and keep `time_series`, `intraday_time_series`, `get_sleep`, `get_bodyweight`, `user_profile_get`... returning the legacy JSON shapes.
* **Typed and robust**: a typed `DataType` enum covers all 42 types, errors map to exception classes, retries use backoff and honour `Retry-After`, and a 401 triggers one refresh-and-retry.

## Install

```bash
pip install google-health               # core (httpx only)
pip install "google-health[webhooks]"   # + signature verification (cryptography)
pip install "google-health[google-auth]"  # + use google.auth / service-account credentials
```

## Quick start

```bash
export GOOGLE_HEALTH_CLIENT_ID=...apps.googleusercontent.com
export GOOGLE_HEALTH_CLIENT_SECRET=...
google-health login --client-secrets client_secret_xxx.json   # JSON from the Cloud console
# or: export GOOGLE_HEALTH_CLIENT_ID=... GOOGLE_HEALTH_CLIENT_SECRET=... && google-health login
google-health whoami           # Google Health user id + legacy Fitbit user id
google-health daily steps --days 7
```

```python
import datetime as dt
from google_health import HealthClient, OAuthCredentials, DataType

client = HealthClient(OAuthCredentials.load("~/.config/google-health/token.json"))

# Per-day totals in the user's local days ([start, end) – end is exclusive)
for day in client.daily(DataType.STEPS, "2026-07-01", "2026-10-29"):   # 120 days → 2 API calls
    print(day.date, day.value)

# 15-minute heart-rate averages
for w in client.windows("heart-rate", "2026-10-28T00:00:00Z", "2026-10-29T00:00:00Z",
                        dt.timedelta(minutes=15)):
    print(w.start, w.value)

# Raw data points – the right filter is built for the data type's kind
for p in client.data_points.list("weight", "2026-01-01", "2026-11-01"):
    print(p["weight"]["weightGrams"])

# Sleep sessions that ended on a given local day
for s in client.data_points.list("sleep", "2026-10-28", "2026-10-29", civil=True):
    print(s["sleep"]["summary"]["minutesAsleep"])

# Write data (returns a long-running Operation)
client.data_points.create("weight", {
    "weightGrams": 72500,
    "sampleTime": {"physicalTime": "2026-10-28T07:00:00Z", "utcOffset": "10800s"},
})

# Exercise as TCX
tcx: bytes = client.data_points.export_exercise_tcx("2026443605080188808")
```

### Async

```python
from google_health import AsyncHealthClient

async with AsyncHealthClient(creds) as client:
    async for p in client.data_points.list("heart-rate", start, end):
        ...
    days = await client.daily("distance", "2026-10-01", "2026-10-29")
```

## Account linking (read this first)

Google Health only returns data for accounts that have been **linked in the Google Health
mobile app**. Until the user installs the app, signs in with the same Google Account and
finishes onboarding, every call fails with `400 FAILED_PRECONDITION: The account is not linked
to Google Health`. The library raises this as `AccountNotLinked` (a `FailedPrecondition`
subclass) so you can show the user a "set up Google Health on your phone" screen:

```python
from google_health import AccountNotLinked
try:
    identity = client.identity()
except AccountNotLinked:
    ...  # tell the user to open the Google Health app and sign in
```

## Authentication

```python
from google_health import OAuthFlow, Scope

flow = OAuthFlow(CLIENT_ID, CLIENT_SECRET, Scope.READ_COMMON,
                 redirect_uri="https://example.com/oauth/callback")
req = flow.authorization_url()          # store req.state and req.code_verifier in the session
# ... redirect the user to req.url, then in your callback:
creds = flow.exchange_code(code, req.code_verifier)
creds.on_refresh = lambda c: db.save_token(user_id, c.to_dict())   # persist rotated tokens
```

`Scope` lists every scope from the discovery document (`ACTIVITY_AND_FITNESS_READ`, `SLEEP_READ`, `HEALTH_METRICS_READ`, `ECG_READ`, ... plus their write-only variants).

For subscriber management, which is project-level, wrap service-account credentials:

```python
from google.oauth2 import service_account
from google_health import GoogleAuthCredentials, HealthClient
sa = service_account.Credentials.from_service_account_file(
    "sa.json", scopes=["https://www.googleapis.com/auth/cloud-platform"])
admin = HealthClient(GoogleAuthCredentials(sa), project="my-project")
```

## Webhooks

```python
from flask import Flask, request
from google_health.webhooks import WebhookReceiver

app = Flask(__name__)
receiver = WebhookReceiver(secret="Bearer my-long-random-secret")

@app.post("/health-webhook")
def health_webhook():
    result = receiver.handle(request.headers, request.get_data())
    for n in result.notifications:           # parsed, signature-verified
        enqueue_sync(n.health_user_id, n.data_type, n.operation, n.intervals)
    return "", result.status_code            # 201 for the verification probe, 204 for data
```

Register the endpoint, then (with the `MANUAL` policy) subscribe individual users:

```python
admin.subscribers.create("https://example.com/health-webhook", "Bearer my-long-random-secret",
                         ["steps", "sleep", "weight"], subscriber_id="prod-hook")
admin.subscribers.rotate_secret("prod-hook", "Bearer new-secret")   # accept both while rotating
```

See [`examples/`](examples/) for Flask and FastAPI receivers.

## Migrating from python-fitbit

```python
# import fitbit
from google_health import fitbit_compat as fitbit

client = fitbit.Fitbit(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                       access_token=tok["access_token"], refresh_token=tok["refresh_token"],
                       expires_at=tok["expires_at"], refresh_cb=save_token,
                       system=fitbit.Fitbit.METRIC)

client.time_series("activities/steps", period="30d")
client.intraday_time_series("activities/heart", base_date="2026-10-28", detail_level="1min")
client.get_sleep("2026-10-28")
client.legacy_user_id()      # old Fitbit encodedId → re-key your database
```

[MIGRATION.md](MIGRATION.md) covers the full mapping table, the semantic differences, and a checklist for the 2 November 2026 cut-over.

## Data types

`DataType` covers all 42 types. Each one carries its `kind`, which is `INTERVAL`, `SAMPLE`, `DAILY`, `SESSION` or `ROLLUP_ONLY`:

| Kind | Types |
|---|---|
| interval | steps, distance, activity-level, floors, altitude, active-energy-burned, basal-energy-burned, active-minutes, active-zone-minutes, sedentary-period, time-in-heart-rate-zone, swim-lengths-data, menstrual-period |
| sample | heart-rate, heart-rate-variability, weight, height, body-fat, blood-glucose, oxygen-saturation, core-body-temperature, vo2-max, run-vo2-max, respiratory-rate-sleep-summary, moods, symptoms, ovulation-test |
| daily | daily-resting-heart-rate, daily-heart-rate-variability, daily-heart-rate-zones, daily-oxygen-saturation, daily-respiratory-rate, daily-sleep-temperature-derivations, daily-vo2-max |
| session | sleep, exercise, electrocardiogram, hydration-log, nutrition-log, irregular-rhythm-notification |
| roll-up only | total-calories, calories-in-heart-rate-zone |

## Status

Version 0.1, alpha. The request and response shapes were built from the official discovery document (revision 2026-10-27) and are covered by 58 unit tests on Python 3.9–3.13 against mocked HTTP. The OAuth flow, `getIdentity`, `dataPoints.list` and `dailyRollUp` have also been exercised against the live API with a real account (October 2026). Please open an issue if a live response differs for other data types.

This is a community project, not affiliated with or endorsed by Google. "Fitbit" and "Google Health" are trademarks of Google LLC.

## License

Apache-2.0
