# Changelog

## 0.1.0 — 2026-10-28

- First release: sync and async clients for every Google Health API v4 method (users, data points, roll-ups, paired devices, subscribers, subscriptions).
- Automatic filter building, roll-up range chunking and pagination.
- OAuth 2.0 with PKCE and token refresh, plus the `google-health` CLI.
- Webhook receiver with Tink/ECDSA signature verification.
- `fitbit_compat` migration layer for `python-fitbit` users.
