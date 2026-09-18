#!/usr/bin/env python3
"""
Withings OAuth 2.0 helper.

Handles:
  - One-time setup (client id/secret, authorize, exchange code for tokens)
  - Loading credentials + tokens from disk
  - Refreshing expired access tokens, persisting the ROTATED refresh token

Storage layout (all inside withings-weight/data/):
  - credentials.json  { "client_id": "...", "client_secret": "...",
                        "redirect_uri": "https://.../withings-callback/",
                        "listen_uri": "http://localhost:8765/callback" }
  - tokens.json       { "access_token": "...", "refresh_token": "...",
                        "expires_at": 1234567890, "userid": 123, "scope": "..." }

Both files are kept out of git (see data/.gitignore) and written chmod 600.

`redirect_uri` and `listen_uri` are deliberately two different things:

  - **redirect_uri** is what Withings knows. It must match a Registered URL on
    the developer application exactly, and it is sent both in the authorize URL
    and again in the token exchange. The dashboard refuses `localhost`, so in
    practice this is an https page.
  - **listen_uri** is where this process actually waits. The registered https
    page is a static relay that forwards the redirect, query string intact, to
    this loopback address.

Three Withings quirks drive the design of this file:

  1. The authorization code is valid for **30 seconds**. A copy-paste flow is
     usually too slow, so `authorize` runs a throwaway HTTP server on the
     loopback address, catches the (relayed) redirect and exchanges the code in
     the request handler itself. `exchange --code` stays as a manual fallback.

  2. The refresh token **rotates on every refresh**. The old one dies 8 hours
     after the new one is issued, so the new refresh token is written to disk
     atomically (temp file + os.replace) *before* the new access token is used.
     Losing that write means re-running the whole authorize flow.

  3. The developer dashboard **rejects localhost** in Registered URLs, hence the
     relay split above.

Environment overrides: WITHINGS_CLIENT_ID, WITHINGS_CLIENT_SECRET.
"""

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
TOKENS_FILE = DATA_DIR / "tokens.json"

AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"

# Only user.metrics is needed for scale measurements.
DEFAULT_SCOPE = "user.metrics"

# Registered on the Withings application. The dashboard refuses localhost in
# "Registered URLs", so this static GitHub Pages relay stands in for it and
# forwards the redirect to DEFAULT_LISTEN_URI. Source: docs/withings-callback/.
DEFAULT_REDIRECT_URI = "https://elbiczel.github.io/sport-skills/withings-callback/"
# Where this process actually listens. The relay page hardcodes this address,
# so changing the port means editing docs/withings-callback/index.html too.
DEFAULT_LISTEN_URI = "http://localhost:8765/callback"

# Withings wrapped-response status codes we care about. status 0 == success.
STATUS_OK = 0
STATUS_BAD_CREDENTIALS = 342   # OAuth credentials are absent or incorrect
STATUS_BAD_TOKEN = 343         # OAuth access token absent or invalid
STATUS_BAD_CODE = 304          # authorization code absent or incorrect
STATUS_RATE_LIMITED = 601      # too many requests
# Token refresh is retried when the API reports one of these.
INVALID_TOKEN_STATUSES = {100, 101, 102, 200, 401, STATUS_BAD_TOKEN}

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class WithingsAuthError(RuntimeError):
    """Credentials/tokens are missing, invalid, or the OAuth flow failed."""


class WithingsAPIError(RuntimeError):
    """The API returned a non-zero `status` in its wrapped response."""

    def __init__(self, status: int, message: str = "", payload: dict | None = None):
        self.status = status
        self.payload = payload or {}
        detail = f" - {message}" if message else ""
        super().__init__(f"Withings API status {status}{detail}")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r") as f:
        return json.load(f)


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON via temp file + rename so a crash can't truncate the tokens.

    Matters most for tokens.json: the refresh token rotates on every refresh
    and a half-written file would strand the skill with no way back in.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with tmp.open("w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save_credentials(client_id: str, client_secret: str,
                     redirect_uri: str = DEFAULT_REDIRECT_URI,
                     listen_uri: str = DEFAULT_LISTEN_URI) -> Path:
    """Persist the developer-app credentials and both callback addresses (chmod 600)."""
    _write_json_atomic(CREDENTIALS_FILE, {
        "client_id": str(client_id).strip(),
        "client_secret": str(client_secret).strip(),
        "redirect_uri": str(redirect_uri).strip(),
        "listen_uri": str(listen_uri).strip(),
    })
    return CREDENTIALS_FILE


def load_credentials() -> dict:
    """Return {client_id, client_secret, redirect_uri, listen_uri}; env wins for the secrets."""
    creds = _read_json(CREDENTIALS_FILE)
    client_id = os.environ.get("WITHINGS_CLIENT_ID") or creds.get("client_id")
    client_secret = os.environ.get("WITHINGS_CLIENT_SECRET") or creds.get("client_secret")
    if not client_id or not client_secret:
        raise WithingsAuthError(
            "Missing Withings credentials. Run `uv run auth.py init` (or set "
            "WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET). Create the developer "
            "app at https://developer.withings.com/dashboard/"
        )
    redirect_uri = str(creds.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip()
    return {
        "client_id": str(client_id).strip(),
        "client_secret": str(client_secret).strip(),
        "redirect_uri": redirect_uri,
        "listen_uri": resolve_listen_uri(creds.get("listen_uri"), redirect_uri),
    }


def resolve_listen_uri(stored: str | None, redirect_uri: str) -> str:
    """Decide where to listen locally, given what was stored and the registered URI.

    A credentials.json written before the relay existed has no `listen_uri`; if
    its redirect_uri is itself loopback, that address is still the right place
    to wait, so the old direct-to-localhost setup keeps working untouched.
    """
    if stored:
        return str(stored).strip()
    if is_loopback(redirect_uri):
        return redirect_uri
    return DEFAULT_LISTEN_URI


def load_tokens() -> dict:
    return _read_json(TOKENS_FILE)


def save_tokens(tokens: dict) -> None:
    """Persist tokens atomically. Always called before the new access token is used."""
    _write_json_atomic(TOKENS_FILE, tokens)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _post_form(url: str, data: dict, headers: dict | None = None) -> dict:
    """POST application/x-www-form-urlencoded and parse the JSON response.

    Overridden by the offline self-test to exercise the status handling.
    """
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise WithingsAuthError(f"HTTP {e.code} from {url}: {body}") from e
    except urllib.error.URLError as e:
        raise WithingsAuthError(f"Could not reach {url}: {e.reason}") from e


def unwrap(payload: dict) -> dict:
    """Return `body` from a Withings response, raising on a non-zero `status`.

    Withings returns HTTP 200 with `{"status": <non-zero>}` for errors, so the
    HTTP status alone is never enough to tell success from failure.
    """
    if not isinstance(payload, dict) or "status" not in payload:
        raise WithingsAPIError(-1, "response had no `status` field", payload)
    status = payload.get("status")
    if status != STATUS_OK:
        raise WithingsAPIError(status, str(payload.get("error", "")), payload)
    body = payload.get("body")
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def build_authorize_url(client_id: str, redirect_uri: str, state: str,
                        scope: str = DEFAULT_SCOPE, demo: bool = False) -> str:
    """Build the Withings consent URL. `state` is required by Withings."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": scope,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if demo:
        params["mode"] = "demo"
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _store_token_response(body: dict) -> dict:
    tokens = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": int(time.time()) + int(body.get("expires_in", 10800)),
        "userid": body.get("userid"),
        "scope": body.get("scope"),
        "token_type": body.get("token_type", "Bearer"),
    }
    save_tokens(tokens)
    return tokens


def exchange_code(code: str, redirect_uri: str | None = None) -> dict:
    """Exchange an authorization code for tokens. The code lives ~30 seconds."""
    creds = load_credentials()
    payload = _post_form(TOKEN_URL, {
        "action": "requesttoken",
        "grant_type": "authorization_code",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "code": code,
        "redirect_uri": redirect_uri or creds["redirect_uri"],
    })
    try:
        body = unwrap(payload)
    except WithingsAPIError as e:
        if e.status == STATUS_BAD_CODE:
            raise WithingsAuthError(
                "Withings rejected the authorization code (status 304). It is only "
                "valid for 30 seconds - re-run `uv run auth.py authorize`."
            ) from e
        raise
    return _store_token_response(body)


def make_exchanger(redirect_uri: str):
    """Return a one-arg exchanger pinned to the REGISTERED redirect_uri.

    The token call must echo the redirect_uri Withings saw in the authorize URL
    - the https relay page - not the loopback address the request arrived on.
    """
    return lambda code: exchange_code(code, redirect_uri=redirect_uri)


def refresh_access_token() -> dict:
    """Refresh the access token and persist the NEW refresh token.

    Withings rotates the refresh token on every call; the previous one stops
    working 8 hours after the new one is issued. The write happens before the
    caller gets the new access token, so a crash can't lose the rotation.
    """
    creds = load_credentials()
    tokens = load_tokens()
    if not tokens.get("refresh_token"):
        raise WithingsAuthError(
            "No refresh_token saved. Run `uv run auth.py authorize` first."
        )
    payload = _post_form(TOKEN_URL, {
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": tokens["refresh_token"],
    })
    try:
        body = unwrap(payload)
    except WithingsAPIError as e:
        raise WithingsAuthError(
            f"Refresh failed (status {e.status}). The refresh token may have expired "
            "(they last 1 year, or die 8h after a rotation you didn't save). "
            "Re-run `uv run auth.py authorize`."
        ) from e
    return _store_token_response(body)


def get_access_token(force_refresh: bool = False) -> str:
    """Return a usable access token, refreshing when it is expired or about to be."""
    tokens = load_tokens()
    if not tokens.get("access_token"):
        raise WithingsAuthError(
            "No tokens saved. Run `uv run auth.py init` (once, to store the client "
            "id/secret) then `uv run auth.py authorize`."
        )
    # Access tokens last 3 hours; refresh 60s early to avoid a race.
    if force_refresh or tokens.get("expires_at", 0) <= int(time.time()) + 60:
        tokens = refresh_access_token()
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# local callback server (beats the 30-second code lifetime)
# ---------------------------------------------------------------------------

_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<title>Withings</title>"
    "<body style=\"font-family:system-ui;margin:3rem;max-width:34rem\">"
    "<h2>{heading}</h2><p>{message}</p></body>"
)


class _CallbackServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, addr, handler, *, expected_state, callback_path, exchange):
        super().__init__(addr, handler)
        self.expected_state = expected_state
        self.callback_path = callback_path
        self.exchange = exchange
        self.result = None
        self.error = None
        self.done = False


class _CallbackHandler(BaseHTTPRequestHandler):
    def _reply(self, code: int, heading: str, message: str) -> None:
        html = _PAGE.format(heading=heading, message=message).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.callback_path:
            # Browsers ask for /favicon.ico etc. Ignore without ending the wait.
            self._reply(404, "Not here", "Waiting for the Withings redirect.")
            return

        qs = urllib.parse.parse_qs(parsed.query)
        state = (qs.get("state") or [""])[0]
        code = (qs.get("code") or [""])[0]
        err = (qs.get("error") or [""])[0]

        if err:
            self.server.error = WithingsAuthError(f"Withings returned error={err}")
            self._reply(400, "Authorization failed", f"Withings returned: {err}")
            self.server.done = True
            return

        # Validate state BEFORE touching the code - this is the CSRF check.
        if not secrets.compare_digest(state, self.server.expected_state):
            self.server.error = WithingsAuthError(
                "State mismatch on the OAuth callback - discarding the code. "
                "Re-run `uv run auth.py authorize`."
            )
            self._reply(400, "State mismatch",
                        "The redirect did not carry the expected state. Ignored.")
            self.server.done = True
            return

        if not code:
            self.server.error = WithingsAuthError("Callback carried no `code`.")
            self._reply(400, "No code", "The redirect carried no authorization code.")
            self.server.done = True
            return

        # Exchange immediately: the code expires 30 seconds after issue.
        try:
            self.server.result = self.server.exchange(code)
            self._reply(200, "Authorized",
                        "Tokens saved. You can close this tab and return to the terminal.")
        except Exception as e:  # noqa: BLE001 - surfaced to the CLI caller
            self.server.error = e
            self._reply(500, "Token exchange failed", str(e))
        self.server.done = True

    def log_message(self, *_args):
        pass  # keep the terminal clean


def run_callback_server(expected_state: str, port: int, path: str = "/callback",
                        timeout: float = 180.0, exchange=None,
                        host: str = "127.0.0.1"):
    """Serve one OAuth redirect on the loopback address and return the tokens.

    Blocks until the callback arrives (or `timeout` seconds pass), then shuts
    the socket down. `exchange` is injectable so the self-test can drive the
    whole path without credentials.
    """
    exchange = exchange or (lambda code: exchange_code(code))
    try:
        server = _CallbackServer((host, port), _CallbackHandler,
                                 expected_state=expected_state,
                                 callback_path=path, exchange=exchange)
    except OSError as e:
        raise WithingsAuthError(
            f"Could not bind {host}:{port} ({e.strerror or e}). Something else is "
            f"using that port - free it, or re-run `init` with a different "
            f"--listen address (and update the port hardcoded in the relay page, "
            f"docs/withings-callback/index.html)."
        ) from e

    server.timeout = 1.0
    deadline = time.monotonic() + timeout
    try:
        while not server.done:
            if time.monotonic() > deadline:
                raise WithingsAuthError(
                    f"Timed out after {timeout:.0f}s waiting for the Withings redirect."
                )
            server.handle_request()
    finally:
        server.server_close()

    if server.error:
        raise server.error
    return server.result


def split_redirect_uri(redirect_uri: str) -> tuple[str, int, str]:
    """Return (host, port, path) for a redirect URI, defaulting the port by scheme."""
    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (parsed.hostname or "", port, parsed.path or "/")


def is_loopback(redirect_uri: str) -> bool:
    host, _, _ = split_redirect_uri(redirect_uri)
    return host in LOOPBACK_HOSTS


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_init(args) -> None:
    client_id = args.client_id or os.environ.get("WITHINGS_CLIENT_ID") \
        or input("Withings Client ID: ").strip()
    client_secret = args.client_secret or os.environ.get("WITHINGS_CLIENT_SECRET") \
        or input("Withings Client Secret: ").strip()
    if not client_id or not client_secret:
        raise WithingsAuthError("Both Client ID and Client Secret are required.")
    path = save_credentials(client_id, client_secret, args.redirect_uri, args.listen)
    print(f"Saved credentials to {path}")
    print(f"Registered URL (must match the Withings app exactly): {args.redirect_uri}")
    print(f"Local listener:                                       {args.listen}")
    if not is_loopback(args.redirect_uri):
        print("\nThe registered URL is the static relay page; it forwards the")
        print("redirect to the local listener above.")


def _cmd_authorize(args) -> None:
    creds = load_credentials()
    redirect_uri = args.redirect_uri or creds["redirect_uri"]
    if args.listen:
        listen_uri = args.listen
    elif args.redirect_uri and is_loopback(args.redirect_uri):
        listen_uri = args.redirect_uri          # redirect straight to the listener
    else:
        listen_uri = creds["listen_uri"]
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(creds["client_id"], redirect_uri, state,
                              scope=args.scope, demo=args.demo)

    if args.manual or not listen_uri:
        print("Open this URL, authorize, then IMMEDIATELY copy the `code` parameter")
        print("from the redirect and run `uv run auth.py exchange --code <CODE>`.")
        print("The code expires 30 seconds after you click Allow.\n")
        print(url)
        return

    _host, port, path = split_redirect_uri(listen_uri)
    print(f"Registered redirect: {redirect_uri}")
    print(f"Listening locally:   {listen_uri}")
    if not is_loopback(redirect_uri):
        print("(the registered page relays the redirect back to the listener)")
    print("\nIf the browser does not open, paste this URL yourself:\n")
    print(url)
    print()
    if not args.no_browser:
        webbrowser.open(url)

    # The exchange must echo the REGISTERED redirect_uri, not the listener.
    tokens = run_callback_server(state, port, path, timeout=args.timeout,
                                 exchange=make_exchanger(redirect_uri))
    print("Tokens saved.")
    print(f"  userid:     {tokens.get('userid')}")
    print(f"  scope:      {tokens.get('scope')}")
    print(f"  expires_at: {tokens['expires_at']} "
          f"({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tokens['expires_at']))})")


def _cmd_exchange(args) -> None:
    code = args.code or input("Paste authorization code: ").strip()
    tokens = exchange_code(code, redirect_uri=args.redirect_uri)
    print("Tokens saved.")
    print(f"  userid:     {tokens.get('userid')}")
    print(f"  expires_at: {tokens['expires_at']}")


def _cmd_refresh(_args) -> None:
    tokens = refresh_access_token()
    print("Refreshed (new refresh token persisted).")
    print(f"  expires_at: {tokens['expires_at']} "
          f"({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tokens['expires_at']))})")


def _cmd_check(_args) -> None:
    load_credentials()  # raises a clear error if missing
    if not load_tokens().get("refresh_token"):
        raise WithingsAuthError(
            "Credentials are stored but no tokens yet. Run `uv run auth.py authorize`."
        )
    from withings import describe_measurement, latest_measurement

    reading = latest_measurement()
    if not reading:
        print("Authenticated, but the account has no weigh-ins in the last 90 days.")
        return
    print("Authenticated. Latest weigh-in:")
    print(f"  {describe_measurement(reading)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Withings OAuth 2.0 helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="Store Client ID / Client Secret / callback URLs")
    p.add_argument("--client-id")
    p.add_argument("--client-secret")
    p.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI,
                   help=f"URL registered on the Withings app (default {DEFAULT_REDIRECT_URI})")
    p.add_argument("--listen", default=DEFAULT_LISTEN_URI,
                   help=f"local address to wait on (default {DEFAULT_LISTEN_URI})")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("authorize",
                       help="Open the consent page and catch the redirect locally")
    p.add_argument("--redirect-uri", help="override the registered redirect URL")
    p.add_argument("--listen", help="override the local listener address")
    p.add_argument("--scope", default=DEFAULT_SCOPE)
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--no-browser", action="store_true",
                   help="do not launch a browser, just print the URL")
    p.add_argument("--manual", action="store_true",
                   help="print the URL only; exchange the code yourself")
    p.add_argument("--demo", action="store_true",
                   help="use Withings' demo account (mode=demo)")
    p.set_defaults(func=_cmd_authorize)

    p = sub.add_parser("exchange", help="Manually exchange an authorization code")
    p.add_argument("--code")
    p.add_argument("--redirect-uri")
    p.set_defaults(func=_cmd_exchange)

    p = sub.add_parser("refresh", help="Force a token refresh")
    p.set_defaults(func=_cmd_refresh)

    p = sub.add_parser("check", help="Verify the stored tokens against the API")
    p.set_defaults(func=_cmd_check)

    args = parser.parse_args()
    try:
        args.func(args)
    except (WithingsAuthError, WithingsAPIError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
