#!/usr/bin/env python3
"""
Strava OAuth helper.

Handles:
  - One-time setup (exchange authorization code for tokens)
  - Loading credentials + tokens from disk
  - Refreshing expired access tokens automatically

Storage layout (all inside strava-activities/data/):
  - credentials.json  { "client_id": "...", "client_secret": "..." }
  - tokens.json       { "access_token": "...", "refresh_token": "...",
                        "expires_at": 1234567890 }

Both files should be kept out of git (see data/.gitignore).
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
TOKENS_FILE = DATA_DIR / "tokens.json"

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
DEFAULT_REDIRECT_URI = "http://localhost/exchange_token"
DEFAULT_SCOPE = "read,activity:read_all,profile:read_all"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r") as f:
        return json.load(f)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2)
    # Best-effort: restrict permissions on files that hold secrets
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_credentials() -> dict:
    creds = _read_json(CREDENTIALS_FILE)
    if not creds.get("client_id") or not creds.get("client_secret"):
        raise RuntimeError(
            f"Missing Strava credentials. Create {CREDENTIALS_FILE} with "
            '{"client_id": "...", "client_secret": "..."} or run '
            "`python auth.py --init` to be prompted."
        )
    return creds


def save_credentials(client_id: str, client_secret: str) -> None:
    _write_json(CREDENTIALS_FILE, {
        "client_id": str(client_id),
        "client_secret": str(client_secret),
    })


def load_tokens() -> dict:
    return _read_json(TOKENS_FILE)


def save_tokens(tokens: dict) -> None:
    _write_json(TOKENS_FILE, tokens)


def build_authorize_url(client_id: str,
                        redirect_uri: str = DEFAULT_REDIRECT_URI,
                        scope: str = DEFAULT_SCOPE) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": scope,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _post_form(url: str, data: dict) -> dict:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for an access + refresh token."""
    creds = load_credentials()
    resp = _post_form(TOKEN_URL, {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
    })
    tokens = {
        "access_token": resp["access_token"],
        "refresh_token": resp["refresh_token"],
        "expires_at": resp["expires_at"],
    }
    save_tokens(tokens)
    return tokens


def refresh_access_token() -> dict:
    """Use the refresh_token to get a new access_token. Saves and returns tokens."""
    creds = load_credentials()
    tokens = load_tokens()
    if not tokens.get("refresh_token"):
        raise RuntimeError(
            "No refresh_token saved. Run the auth setup flow first."
        )
    resp = _post_form(TOKEN_URL, {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "grant_type": "refresh_token",
    })
    tokens.update({
        "access_token": resp["access_token"],
        "refresh_token": resp["refresh_token"],
        "expires_at": resp["expires_at"],
    })
    save_tokens(tokens)
    return tokens


def get_access_token() -> str:
    """Return a valid access token, refreshing if necessary."""
    tokens = load_tokens()
    if not tokens.get("access_token"):
        raise RuntimeError("No access_token saved. Run the auth setup flow first.")
    # Refresh 60s before actual expiry to avoid races.
    if tokens.get("expires_at", 0) <= int(time.time()) + 60:
        tokens = refresh_access_token()
    return tokens["access_token"]


def _cmd_init(args: argparse.Namespace) -> None:
    client_id = args.client_id or input("Strava Client ID: ").strip()
    client_secret = args.client_secret or input("Strava Client Secret: ").strip()
    save_credentials(client_id, client_secret)
    print(f"Saved credentials to {CREDENTIALS_FILE}")


def _cmd_authorize(args: argparse.Namespace) -> None:
    creds = load_credentials()
    url = build_authorize_url(
        creds["client_id"],
        redirect_uri=args.redirect_uri,
        scope=args.scope,
    )
    print("Open this URL in your browser and click Authorize:")
    print()
    print(url)
    print()
    print("After clicking Authorize you'll be redirected to a URL like:")
    print(f"  {args.redirect_uri}?state=&code=<CODE>&scope=...")
    print("The page may fail to load - that's fine. Copy the `code` value.")


def _cmd_exchange(args: argparse.Namespace) -> None:
    code = args.code or input("Paste authorization code: ").strip()
    tokens = exchange_code(code)
    print("Tokens saved.")
    print(f"  access_token expires_at: {tokens['expires_at']}")


def _cmd_refresh(_: argparse.Namespace) -> None:
    tokens = refresh_access_token()
    print("Refreshed.")
    print(f"  access_token expires_at: {tokens['expires_at']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Strava OAuth helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Save Client ID and Client Secret")
    p_init.add_argument("--client-id")
    p_init.add_argument("--client-secret")
    p_init.set_defaults(func=_cmd_init)

    p_auth = sub.add_parser("authorize", help="Print the authorization URL")
    p_auth.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    p_auth.add_argument("--scope", default=DEFAULT_SCOPE)
    p_auth.set_defaults(func=_cmd_authorize)

    p_ex = sub.add_parser("exchange", help="Exchange authorization code for tokens")
    p_ex.add_argument("--code")
    p_ex.set_defaults(func=_cmd_exchange)

    p_rf = sub.add_parser("refresh", help="Force-refresh the access token")
    p_rf.set_defaults(func=_cmd_refresh)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
