# Migrating from the Fitbit Web API / python-fitbit

The legacy Fitbit Web API is retired: support ended on 30 September 2026, and the API and its OAuth endpoints shut down on **2 November 2026**. Its replacement is the Google Health API v4 (`health.googleapis.com`).

## Checklist

1. **Create a Google Cloud OAuth client** and enable the Google Health API. Fitbit client ids and secrets do not work with the new API.
2. **Re-consent every user.** Fitbit access and refresh tokens cannot be exchanged for Google tokens, so each user goes through Google OAuth once (`OAuthFlow`).
3. **Map user ids.** After a user re-consents, call `client.users.get_identity()` (or `fitbit_compat.Fitbit.legacy_user_id()`). It returns `legacyUserId`, the old Fitbit `encodedId`, which you can use to attach the new `healthUserId` to your existing rows.
4. **Swap the import** if you use `python-fitbit`:
   `from google_health import fitbit_compat as fitbit`
5. **Replace Fitbit subscriptions** (the old Subscriber API) with Google Health webhooks. Use `client.subscribers.create(...)` together with `webhooks.WebhookReceiver`.
6. **Check units.** The compat layer converts to `en_US` (miles, lb, ft) by default, the same as `python-fitbit`. Pass `system=Fitbit.METRIC` for km, kg and m.

## Endpoint mapping

| python-fitbit | Legacy endpoint | compat layer uses |
|---|---|---|
| `user_profile_get()` | `/1/user/-/profile.json` | `users.getIdentity` + `getProfile` + `getSettings` |
| `get_devices()` | `/1/user/-/devices.json` | `users.pairedDevices.list` |
| `time_series("activities/steps")` | `/activities/steps/date/...` | `dataPoints:dailyRollUp` (`steps`) |
| `time_series("activities/distance")` | | `dailyRollUp` (`distance`, mm → km/mi) |
| `time_series("activities/floors")` | | `dailyRollUp` (`floors`) |
| `time_series("activities/elevation")` | | `dailyRollUp` (`altitude`, gain mm → m/ft) |
| `time_series("activities/calories")` | | `dailyRollUp` (`total-calories`) |
| `time_series("activities/activityCalories")` | | `dailyRollUp` (`active-energy-burned`) |
| `time_series("activities/minutesSedentary")` | | `dailyRollUp` (`sedentary-period`) |
| `time_series("activities/tracker/…")` | tracker-only series | same, with `dataSourceFamily=google-wearables` |
| `time_series("activities/heart")` | resting HR per day | `dataPoints.list` (`daily-resting-heart-rate`) |
| `time_series("body/weight")`, `("body/fat")` | | `dailyRollUp` (`weight`, `body-fat`) |
| `intraday_time_series(...)` | `/1/user/-/activities/.../1d/1min.json` | `dataPoints:rollUp` with 1s/1min/5min/15min windows, in the user's time zone |
| `get_sleep(date)` | `/1.2/user/-/sleep/date/{date}.json` | `dataPoints.list` (`sleep`, `civil_end_time` on that day) |
| `get_bodyweight(...)`, `get_bodyfat(...)` | `/body/log/weight/...` | `dataPoints.list` (`weight` / `body-fat`) |
| `activities(date)` | `/activities/date/{date}.json` | several `dailyRollUp` calls (summary only) |

These calls are not covered by the compat layer, so use the native client for them:

* food logging: `nutrition-log` and `hydration-log` data types
* exercise logs and TCX: the `exercise` data type and `export_exercise_tcx`
* ECG: `electrocardiogram`
* SpO₂, HRV, breathing rate and skin temperature: the `daily-*` types
* friends, badges, goals and alarms: these have no equivalent in the Google Health API

## Semantic differences to be aware of

* **De-duplication.** The legacy API picked one source per day. In the new API, `dataPoints.list` returns points from *all* sources, while `reconcile` and the roll-ups merge them. If numbers look doubled, compare `data_source_family="google-wearables"` with the default.
* **Time.** Legacy endpoints spoke only local time. The new API returns both a UTC instant and civil (local) time. Use `civil=True` on `list()` when you think in user-local days.
* **Integers come back as strings** (`"countSum": "8421"`, int64 in JSON). The `summaries` helpers and `client.daily()` convert them for you.
* **Rate limits** are Google Cloud quotas, not the Fitbit 150 requests per hour per user. 429 responses are retried with backoff automatically.
* **Roll-up limits:** 14 days for `heart-rate`, `active-minutes`, `total-calories` and `calories-in-heart-rate-zone`, and 90 days for everything else. The client splits longer ranges for you.
