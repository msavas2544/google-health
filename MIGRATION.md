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

# Google Fit REST API

The Google Fit APIs (Android and REST) are deprecated in 2026. New sign-ups closed on 1 May 2024, and Google recommends the Google Health API for server-side access. `google_health.googlefit_compat.GoogleFit` answers the read calls most Fit integrations use and returns the Fit JSON shapes.

## Checklist

1. **Enable the Google Health API** in your Cloud project and request its scopes (`Scope.READ_COMMON` covers activity, body metrics and sleep). Fit scopes (`fitness.activity.read`, ...) and Fit tokens do not work.
2. **Re-consent every user** through Google OAuth (`OAuthFlow`). The user also needs the Google Health app set up on their phone, otherwise calls fail with `AccountNotLinked`.
3. **Swap the service object**: replace `build("fitness", "v1", ...)` with `GoogleFit(HealthClient(credentials))`. The `users().dataset().aggregate(...)`, `users().dataSources().list/get(...)`, `users().dataSources().datasets().get(...)` and `users().sessions().list(...)` chains and `.execute()` keep working. You can also call `aggregate(body)`, `get_dataset(...)` and `list_sessions(...)` directly.
4. **Check the unsupported list** below and move those parts to the native client.

## Data type mapping

| Fit data type | Google Health data type | Aggregate result | Unit / notes |
|---|---|---|---|
| `com.google.step_count.delta` | `steps` | same type, `intVal` | steps |
| `com.google.distance.delta` | `distance` | same type, `fpVal` | metres (from mm) |
| `com.google.calories.expended` | `total-calories` | same type, `fpVal` | kcal; datasets come from 15-minute roll-up windows |
| `com.google.active_minutes` | `active-minutes` | same type, `intVal` | minutes, all activity levels summed; datasets from roll-up windows |
| `com.google.heart_minutes` | `active-zone-minutes` | `heart_minutes.summary` (intensity, duration) | Active Zone Minutes; cardio and peak minutes count double, like Heart Points |
| `com.google.heart_rate.bpm` | `heart-rate` | `heart_rate.summary` (avg, max, min) | bpm |
| `com.google.weight` | `weight` | `weight.summary` (avg, max, min) | kg |
| `com.google.height` | `height` | `height.summary` | m |
| `com.google.body.fat.percentage` | `body-fat` | `body.fat.percentage.summary` | % |
| `com.google.oxygen_saturation` | `oxygen-saturation` | `oxygen_saturation.summary` (avg, max, min) | %; only the first field is filled |
| `com.google.blood_glucose` | `blood-glucose` | `blood_glucose.summary` (avg, max, min) | mmol/L (mg/dL ÷ 18.016); only the first field is filled |
| `com.google.hydration` | `hydration-log` | same type, `fpVal` | litres |
| `com.google.nutrition` | `nutrition-log` | `nutrition.summary` (nutrients map, meal_type) | `calories`, `fat.*`, `carbs.total`, `protein`, `sugar`, `dietary_fiber` in g; `sodium`, `potassium`, `cholesterol`, `calcium`, `iron`, `vitamin_c` in mg |
| `com.google.activity.segment` | `exercise` | `activity.summary` (activity, duration ms, num_segments) | Exercise types mapped to Fit activity integers; unmapped types become 108 (other) |
| `com.google.sleep.segment` | `sleep` stages | not aggregatable | 1 awake/restless, 2 sleep, 3 out of bed, 4 light, 5 deep, 6 REM |

Data source ids such as `derived:com.google.step_count.delta:com.google.android.gms:estimated_steps` are accepted; only the data type part is used.

## Requests

| Fit call | How it is answered |
|---|---|
| `dataset:aggregate` + `bucketByTime.durationMillis` | `dataPoints:rollUp` with that window (one call per run of equal-length buckets) |
| `dataset:aggregate` + `bucketByTime.period` (day, week, month) | Buckets stepped in civil time of `timeZoneId` (UTC if omitted), DST-aware |
| `bucketBySession`, `bucketByActivitySegment` | Exercise (and, for sessions, sleep) intervals become the buckets |
| `bucketByActivityType` | Exercises grouped by Fit activity; additive types only (steps, distance, calories, minutes, hydration, nutrition) |
| `dataSources.list` / `get` | A synthetic derived source per supported type, with Fit field descriptors |
| `dataSources.datasets.get` | `dataPoints.list` converted point by point, sorted oldest first; `limit` keeps the newest points |
| `sessions.list` | `exercise` and `sleep` data points (sleep as activity type 72) |

## Not covered

* Writes: `dataSources.create`, `datasets.patch`, `sessions.update` and deletes. Use `client.data_points.create(...)`.
* Data types with no Google Health equivalent here: `calories.bmr`, `speed`, `step_count.cadence`, cycling cadence and wheel speed, `power.sample`, location samples and bounding boxes, `blood_pressure`, `body.temperature`, and the cycle-tracking types (`menstruation`, `cervical_*`, `ovulation_test`, `vaginal_spotting`).
* `deletedSession`, `includeDeleted` and change-tracking fields (`modifiedTimeMillis` is filled from `updateTime` where available).
* Pagination tokens. `datasets.get` and `sessions.list` return everything in the range in one response.
* Raw multi-field values for SpO₂ and blood glucose (oxygen flow, specimen source and similar fields).

## Semantic differences

* **Aggregation and de-duplication** come from the Google Health API, so totals can differ from what Fit reported for the same day.
* **Time units.** Fit uses milliseconds for buckets and sessions, and nanoseconds for points. The layer converts with integer arithmetic, and all of these are returned as strings, like Fit.
* **Roll-up limits.** One roll-up window cannot exceed 14 days for heart rate, active minutes and calories, or 90 days for the rest, so `period: month` fails for those types.
* **Not validated against live Fit data.** The author had no Fit account with data. The shapes follow the published references and the mocked tests.
