"""Tiny command-line interface: ``google-health login | whoami | daily | points``."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from .auth import OAuthCredentials, OAuthFlow, load_client_secrets
from .client import HealthClient
from .errors import GoogleHealthError
from .types import DataType, Scope

DEFAULT_TOKEN = Path(os.environ.get("GOOGLE_HEALTH_TOKEN",
                                    Path.home() / ".config" / "google-health" / "token.json"))


def _client(args: argparse.Namespace) -> HealthClient:
    path = Path(args.token)
    if not path.exists():
        sys.exit(f"No token at {path}. Run `google-health login` first.")
    return HealthClient(OAuthCredentials.load(path))


def cmd_login(args: argparse.Namespace) -> None:
    client_id = args.client_id or os.environ.get("GOOGLE_HEALTH_CLIENT_ID")
    secret = args.client_secret or os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET")
    secrets_file = args.client_secrets or os.environ.get("GOOGLE_HEALTH_CLIENT_SECRETS")
    if secrets_file:
        client_id, secret = load_client_secrets(secrets_file)
    if not client_id:
        sys.exit("--client-secrets FILE, or --client-id (or GOOGLE_HEALTH_CLIENT_ID) is required")
    scopes = args.scope or list(Scope.READ_COMMON)
    flow = OAuthFlow(client_id, secret, scopes)
    creds = flow.run_local_server(port=args.port, open_browser=not args.no_browser,
                                  print_fn=lambda m: print(m, file=sys.stderr))
    creds.save(args.token)
    print(f"Saved token to {args.token}", file=sys.stderr)


def cmd_whoami(args: argparse.Namespace) -> None:
    with _client(args) as c:
        print(json.dumps(c.users.get_identity(), indent=2))


def cmd_daily(args: argparse.Namespace) -> None:
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() + dt.timedelta(days=1)
    start = dt.date.fromisoformat(args.start) if args.start else end - dt.timedelta(days=args.days)
    with _client(args) as c:
        rows = c.daily(args.data_type, start, end)
    if args.json:
        print(json.dumps([{"date": r.date.isoformat(), "value": r.value} for r in rows], indent=2))
        return
    if not rows:
        print(f"No {args.data_type} data between {start} and {end} (end exclusive).",
              file=sys.stderr)
    for r in rows:
        print(f"{r.date}  {'' if r.value is None else round(r.value, 2)}")


def cmd_points(args: argparse.Namespace) -> None:
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=args.days)
    with _client(args) as c:
        for p in c.data_points.list(args.data_type, start, end, limit=args.limit):
            print(json.dumps(p))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="google-health", description=__doc__)
    p.add_argument("--token", default=str(DEFAULT_TOKEN), help="token file (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="authorize via browser and save a token")
    s.add_argument("--client-id")
    s.add_argument("--client-secret")
    s.add_argument("--client-secrets", metavar="FILE",
                   help="client_secret_*.json downloaded from Google Cloud console")
    s.add_argument("--scope", action="append", help="repeatable; default: common read scopes")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("whoami", help="show Google Health + legacy Fitbit user ids")
    s.set_defaults(func=cmd_whoami)

    types = [d.value for d in DataType]
    s = sub.add_parser("daily", help="per-day roll-up, e.g. `daily steps --days 7`")
    s.add_argument("data_type", choices=types, metavar="DATA_TYPE")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--start")
    s.add_argument("--end", help="exclusive end date")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_daily)

    s = sub.add_parser("points", help="raw data points as JSON lines")
    s.add_argument("data_type", choices=types, metavar="DATA_TYPE")
    s.add_argument("--days", type=int, default=1)
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_points)
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except GoogleHealthError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":  # pragma: no cover
    main()
