# Changelog

## 0.2.0 — 2026-10-29

- New `googlefit_compat` module: a Google Fit REST API look-alike backed by the Google Health API. Covers `dataset:aggregate` (time, period, session, activity-segment and activity-type buckets), `dataSources.list`/`get`, `dataSources.datasets.get` and `sessions.list`, with a `googleapiclient`-style `users()...execute()` chain.
- 15 Fit data types mapped, with unit conversions (mm → m, g → kg, mg/dL → mmol/L, ml → l) and nanosecond/millisecond timestamps.
- MIGRATION.md: Google Fit section with the mapping table and known gaps.

## 0.1.1 — 2026-10-29

- `login --client-secrets FILE` accepts the `client_secret_*.json` downloaded from the Google Cloud console.
- `AccountNotLinked` error for accounts that haven't completed Google Health app onboarding.
- `daily` CLI command reports when a range has no data.
- Verified end to end against the live API with a real account.

## 0.1.0 — 2026-10-28

- First release: sync and async clients for every Google Health API v4 method (users, data points, roll-ups, paired devices, subscribers, subscriptions).
- Automatic filter building, roll-up range chunking and pagination.
- OAuth 2.0 with PKCE and token refresh, plus the `google-health` CLI.
- Webhook receiver with Tink/ECDSA signature verification.
- `fitbit_compat` migration layer for `python-fitbit` users.
