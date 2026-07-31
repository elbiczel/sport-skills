#!/usr/bin/env python3
"""Hevy API key storage.

The Hevy public API uses a single long-lived API key sent in the `api-key`
header - no OAuth flow. The key is generated at https://hevy.com/settings?developer
and is only available to Hevy Pro accounts.
"""

import json
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"

API_BASE = "https://api.hevyapp.com/v1"


class HevyAuthError(RuntimeError):
    pass


def save_api_key(api_key: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps({"api_key": api_key.strip()}, indent=2))
    CREDENTIALS_FILE.chmod(0o600)
    return CREDENTIALS_FILE


def get_api_key() -> str:
    """Resolve the API key from the environment or the stored credentials file."""
    env = os.environ.get("HEVY_API_KEY")
    if env:
        return env.strip()

    if not CREDENTIALS_FILE.exists():
        raise HevyAuthError(
            "Missing Hevy API key. Run `uv run auth.py init` (or set HEVY_API_KEY). "
            "Get a key at https://hevy.com/settings?developer (requires Hevy Pro)."
        )

    data = json.loads(CREDENTIALS_FILE.read_text())
    key = (data.get("api_key") or "").strip()
    if not key:
        raise HevyAuthError("credentials.json has no api_key. Re-run `uv run auth.py init`.")
    return key


def auth_headers(json_body: bool = False) -> dict:
    headers = {"api-key": get_api_key(), "Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _cli(argv):
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        print("Commands:\n  init    Store an API key\n  check   Verify the stored key works")
        return 0

    cmd = argv[0]

    if cmd == "init":
        key = input("Hevy API key (https://hevy.com/settings?developer): ").strip()
        if not key:
            print("No key entered; nothing saved.", file=sys.stderr)
            return 1
        path = save_api_key(key)
        print(f"Saved to {path}")
        return 0

    if cmd == "check":
        from hevy import get_user_info, workout_count

        info = get_user_info()
        who = info.get("name") or info.get("username") or "(unknown)"
        print(f"Authenticated as: {who}  {info.get('url', '')}".rstrip())
        print(f"Workouts on account: {workout_count()}")
        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
