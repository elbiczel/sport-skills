#!/usr/bin/env python3
"""
Offline self-test for the withings-weight skill.

Runs with no credentials and makes no network calls: every HTTP boundary is
stubbed. It covers the parts that would otherwise only be exercised against the
live account - unit scaling, pagination, first-of-day marking, the weekly
Friday-first selection, refresh-token rotation, wrapped-status errors and the
local OAuth callback server.

    uv run selftest.py
"""

import json
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402
import withings  # noqa: E402

TZ = withings.resolve_timezone()
FAILURES = []
PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label} {detail}")


def section(title: str) -> None:
    print(f"\n{title}")


def ts(text: str) -> int:
    """Unix seconds for a local 'YYYY-MM-DD HH:MM' in the skill's timezone."""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


def group(grpid: int, when: str, weight: float, *, fat_ratio=None, extras=True) -> dict:
    """Build a fake measure group the way the API encodes one (value * 10^unit)."""
    measures = [{"value": int(round(weight * 1000)), "type": 1, "unit": -3}]
    if fat_ratio is not None:
        measures.append({"value": int(round(fat_ratio * 100)), "type": 6, "unit": -2})
    if extras:
        measures += [
            {"value": int(round(weight * 0.84 * 1000)), "type": 5, "unit": -3},
            {"value": int(round(weight * 0.16 * 1000)), "type": 8, "unit": -3},
            {"value": int(round(weight * 0.80 * 1000)), "type": 76, "unit": -3},
            {"value": 3120, "type": 88, "unit": -3},
            {"value": int(round(weight * 0.58 * 1000)), "type": 77, "unit": -3},
        ]
    return {"grpid": grpid, "date": when if isinstance(when, int) else ts(when),
            "attrib": 0, "category": 1, "model": "Body Comp", "measures": measures}


# Fixture: W36 has two readings on one Friday; W37 has no Friday at all
# (Mon + two on Sat); W38 has a single Friday reading.
FIXTURE_GROUPS = [
    group(101, "2026-09-04 06:40", 75.10, fat_ratio=16.4),   # W36 Fri, early
    group(102, "2026-09-04 19:20", 75.90, fat_ratio=17.1),   # W36 Fri, evening
    group(103, "2026-09-07 06:45", 74.90, fat_ratio=16.2),   # W37 Mon
    group(104, "2026-09-12 08:10", 75.40, fat_ratio=16.6),   # W37 Sat, early
    group(105, "2026-09-12 21:00", 76.00, fat_ratio=17.3),   # W37 Sat, evening
    group(106, "2026-09-18 06:50", 74.80, fat_ratio=16.0),   # W38 Fri
]


# ---------------------------------------------------------------------------

def test_unit_scaling() -> None:
    section("unit scaling (real value = value * 10^unit)")
    check("7500 * 10^-2 == 75.0", withings.measure_value({"value": 7500, "unit": -2}) == 75.0)
    check("20 * 10^-1 == 2.0", withings.measure_value({"value": 20, "unit": -1}) == 2.0)
    check("75100 * 10^-3 == 75.1",
          abs(withings.measure_value({"value": 75100, "unit": -3}) - 75.1) < 1e-9)
    check("164 * 10^-1 == 16.4 (fat ratio %)",
          abs(withings.measure_value({"value": 164, "unit": -1}) - 16.4) < 1e-9)


def test_parsing() -> None:
    section("group parsing and local time")
    reading = withings.parse_group(FIXTURE_GROUPS[0], TZ)
    check("date is local", reading["date"] == "2026-09-04", reading["date"])
    check("weekday is Fri", reading["weekday"] == "Fri", reading["weekday"])
    check("time is 06:40", reading["time"] == "06:40", reading["time"])
    check("weight decoded", abs(reading["weight"] - 75.10) < 1e-6, str(reading["weight"]))
    check("fat_ratio decoded", abs(reading["fat_ratio"] - 16.4) < 1e-6,
          str(reading["fat_ratio"]))
    check("bone_mass decoded", abs(reading["bone_mass"] - 3.12) < 1e-6,
          str(reading["bone_mass"]))
    check("missing field is None",
          withings.parse_group(group(900, "2026-09-04 06:40", 70.0, extras=False),
                               TZ)["muscle_mass"] is None)
    line = withings.describe_measurement(reading)
    check("describe_measurement has weight + composition",
          "75.10 kg" in line and "fat 16.4%" in line and "bone 3.12 kg" in line, line)


def test_pagination_merge() -> None:
    section("pagination merge")
    page1 = [FIXTURE_GROUPS[0], FIXTURE_GROUPS[1]]
    page2 = [FIXTURE_GROUPS[1], FIXTURE_GROUPS[2]]  # 102 repeated across the boundary
    merged = withings.merge_measure_pages([page1, page2])
    check("overlapping grpid deduped", len(merged) == 3, str(len(merged)))
    check("order preserved", [g["grpid"] for g in merged] == [101, 102, 103],
          str([g["grpid"] for g in merged]))


def test_paginated_fetch() -> None:
    section("paginated fetch (stubbed HTTP, more/offset loop)")
    calls = []

    def fake_post(url, data, headers=None):
        calls.append(dict(data))
        if url == auth.TOKEN_URL:
            return {"status": 0, "body": {"access_token": "AT", "refresh_token": "RT",
                                          "expires_in": 10800}}
        offset = int(data.get("offset", 0))
        if offset == 0:
            return {"status": 0, "body": {"timezone": "Europe/Zurich", "more": 1,
                                          "offset": 3,
                                          "measuregrps": FIXTURE_GROUPS[:3]}}
        return {"status": 0, "body": {"timezone": "Europe/Zurich", "more": 0,
                                      "measuregrps": FIXTURE_GROUPS[3:]}}

    with stubbed(_post_form=fake_post, get_access_token=lambda force_refresh=False: "AT"):
        groups, body_tz = withings.get_measure_groups(meastypes=[1, 6])

    check("followed pagination to the end", len(groups) == 6, str(len(groups)))
    check("made exactly 2 measure calls", len(calls) == 2, str(len(calls)))
    check("second call carried offset", calls[1].get("offset") == 3, str(calls[1]))
    check("meastypes sent as comma list", calls[0]["meastypes"] == "1,6",
          calls[0]["meastypes"])
    check("category=1 (real measures)", calls[0]["category"] == 1, str(calls[0]))
    check("timezone read from body", body_tz == "Europe/Zurich", str(body_tz))


def test_first_of_day() -> None:
    section("first-of-day marking")
    readings = withings.parse_measure_groups(FIXTURE_GROUPS, TZ)
    check("newest first", readings[0]["grpid"] == 106, str(readings[0]["grpid"]))
    by_id = {r["grpid"]: r for r in readings}
    check("06:40 Friday reading is first of day", by_id[101]["first_of_day"] is True)
    check("19:20 Friday reading is not", by_id[102]["first_of_day"] is False)
    check("both counted in day_count", by_id[102]["day_count"] == 2,
          str(by_id[102]["day_count"]))
    check("single reading day is first of day", by_id[103]["first_of_day"] is True)
    check("marker shown only on multi-reading days",
          "* first of 2 today" in withings.describe_measurement(by_id[101])
          and "first of" not in withings.describe_measurement(by_id[103]))


def test_weekly_selection() -> None:
    section("weekly selection (Friday first, fallback, delta, mean)")
    readings = withings.parse_measure_groups(FIXTURE_GROUPS, TZ)
    rows = withings.weekly_rows(readings, weeks=8)
    check("three ISO weeks", len(rows) == 3, str(len(rows)))
    check("oldest first", [r["label"] for r in rows] == ["2026-W36", "2026-W37", "2026-W38"],
          str([r["label"] for r in rows]))

    w36, w37, w38 = rows
    check("W36 picked the EARLIER of two Friday readings",
          w36["date"] == "2026-09-04" and w36["time"] == "06:40"
          and abs(w36["weight"] - 75.10) < 1e-6, json.dumps(w36, default=str))
    check("W36 note says Friday", w36["note"].startswith("Friday"), w36["note"])
    check("W36 mean averages both readings",
          abs(w36["mean_weight"] - 75.50) < 1e-6, str(w36["mean_weight"]))
    check("W36 has no delta (first row)", w36["delta"] is None)

    check("W37 fell back to the latest day (Sat), first reading",
          w37["date"] == "2026-09-12" and w37["time"] == "08:10"
          and abs(w37["weight"] - 75.40) < 1e-6, json.dumps(w37, default=str))
    check("W37 note flags the substitution",
          w37["note"] == "no Friday reading, used Sat", w37["note"])
    check("W37 mean over all 3 readings (rounded to 2dp)",
          w37["mean_weight"] == round((74.90 + 75.40 + 76.00) / 3, 2),
          str(w37["mean_weight"]))
    check("W37 delta vs previous row",
          abs(w37["delta"] - (75.40 - 75.10)) < 1e-6, str(w37["delta"]))

    check("W38 Friday reading", w38["date"] == "2026-09-18"
          and abs(w38["weight"] - 74.80) < 1e-6, json.dumps(w38, default=str))
    check("W38 delta is negative",
          abs(w38["delta"] - (74.80 - 75.40)) < 1e-6, str(w38["delta"]))
    check("--weeks trims to the most recent",
          [r["label"] for r in withings.weekly_rows(readings, weeks=2)]
          == ["2026-W37", "2026-W38"])
    lines = withings.format_weekly_rows(rows)
    check("formatted table has header + one line per week", len(lines) == 5, str(len(lines)))
    check("formatted delta is signed", "-0.60" in lines[-1], lines[-1])

    # Morning mean: first-of-day readings before MORNING_CUTOFF_HOUR only.
    for row in rows:
        expected = [r for r in readings
                    if r["datetime"].isocalendar()[:2] == (row["iso_year"], row["iso_week"])
                    and r["first_of_day"] and r["weight"] is not None
                    and r["datetime"].hour < withings.MORNING_CUTOFF_HOUR]
        check(f"{row['label']} morning count matches first-of-day-before-cutoff",
              row["n_mornings"] == len(expected), f"{row['n_mornings']} vs {len(expected)}")
        if expected:
            want = round(sum(r["weight"] for r in expected) / len(expected), 2)
            check(f"{row['label']} morning mean", row["morning_mean"] == want,
                  f"{row['morning_mean']} vs {want}")
    check("W36 evening Friday reading is excluded from the morning mean",
          w36["n_mornings"] == 1 and abs(w36["morning_mean"] - 75.10) < 1e-6,
          json.dumps(w36["mornings"]))
    check("first row has no morning delta", rows[0]["morning_delta"] is None)
    with_prev = [r for r in rows[1:] if r["morning_mean"] is not None]
    check("later rows carry a morning delta", all(r["morning_delta"] is not None for r in with_prev))
    trend = withings.format_weekly_trend(rows)
    check("trend table: header + weeks + footer", len(trend) == 2 + len(rows) + 3, str(len(trend)))
    check("trend table has a fat% column and the DEXA caveat",
          "fat%" in trend[0] and "DEXA" in trend[-1], trend[-1])
    for row in rows:
        fats = [m["fat_ratio"] for m in row["mornings"] if m["fat_ratio"] is not None]
        want = round(sum(fats) / len(fats), 1) if fats else None
        check(f"{row['label']} morning fat mean", row["morning_fat_mean"] == want
              or (want is not None and abs(row["morning_fat_mean"] - want) <= 0.1),
              f"{row['morning_fat_mean']} vs {want}")
    check("trend table lists weekday and weight", "Fri 74.80" in trend[4], trend[4])


# ---------------------------------------------------------------------------

class stubbed:
    """Context manager swapping attributes on the `auth` module for a test."""

    def __init__(self, **attrs):
        self.attrs = attrs
        self.saved = {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.saved[name] = getattr(auth, name)
            setattr(auth, name, value)
        return self

    def __exit__(self, *_exc):
        for name, value in self.saved.items():
            setattr(auth, name, value)
        return False


def test_wrapped_status() -> None:
    section("wrapped status handling (HTTP 200 + non-zero status is an error)")
    check("status 0 returns body",
          auth.unwrap({"status": 0, "body": {"a": 1}}) == {"a": 1})
    check("status 0 with no body returns {}", auth.unwrap({"status": 0}) == {})
    for status in (342, 343, 601):
        try:
            auth.unwrap({"status": status})
            check(f"status {status} raises", False, "no exception")
        except auth.WithingsAPIError as e:
            check(f"status {status} raises WithingsAPIError", e.status == status)
    try:
        auth.unwrap({"body": {}})
        check("missing status raises", False, "no exception")
    except auth.WithingsAPIError:
        check("missing status raises", True)


def test_token_rotation(tmp: Path) -> None:
    section("refresh-token rotation is persisted atomically")
    creds = tmp / "credentials.json"
    tokens = tmp / "tokens.json"
    creds.write_text(json.dumps({"client_id": "CID", "client_secret": "CSEC",
                                 "redirect_uri": auth.DEFAULT_REDIRECT_URI}))
    tokens.write_text(json.dumps({"access_token": "AT1", "refresh_token": "RT1",
                                  "expires_at": 0}))

    def fake_post(url, data, headers=None):
        check("refresh sends grant_type=refresh_token",
              data.get("grant_type") == "refresh_token", str(data))
        check("refresh sends the OLD refresh token",
              data.get("refresh_token") == "RT1", str(data))
        return {"status": 0, "body": {"access_token": "AT2", "refresh_token": "RT2",
                                      "expires_in": 10800, "userid": 42,
                                      "scope": "user.metrics"}}

    with stubbed(CREDENTIALS_FILE=creds, TOKENS_FILE=tokens, _post_form=fake_post):
        result = auth.refresh_access_token()
        on_disk = json.loads(tokens.read_text())
        mode = tokens.stat().st_mode & 0o777
        no_temp = not list(tmp.glob("*.tmp*"))

    check("returned the new access token", result["access_token"] == "AT2")
    check("NEW refresh token written to disk", on_disk["refresh_token"] == "RT2",
          on_disk.get("refresh_token"))
    check("expiry computed from expires_in", on_disk["expires_at"] > time.time() + 10000)
    check("userid stored", on_disk["userid"] == 42)
    check("tokens.json is chmod 600", mode == 0o600, oct(mode))
    check("no temp file left behind", no_temp)


def test_refresh_failure(tmp: Path) -> None:
    section("refresh failure gives a clear message, not a traceback")
    creds = tmp / "credentials.json"
    tokens = tmp / "tokens2.json"
    tokens.write_text(json.dumps({"access_token": "AT1", "refresh_token": "RT1",
                                  "expires_at": 0}))

    def fake_post(url, data, headers=None):
        return {"status": 401, "error": "invalid_grant"}

    with stubbed(CREDENTIALS_FILE=creds, TOKENS_FILE=tokens, _post_form=fake_post):
        try:
            auth.refresh_access_token()
            check("raises on a rejected refresh", False, "no exception")
        except auth.WithingsAuthError as e:
            check("raises WithingsAuthError naming the fix",
                  "authorize" in str(e) and "401" in str(e), str(e))
        old = json.loads(tokens.read_text())
    check("failed refresh left the old token intact", old["refresh_token"] == "RT1")


def test_auto_refresh_retry(tmp: Path) -> None:
    section("API retries once after an invalid-token status (343)")
    creds = tmp / "credentials.json"
    tokens = tmp / "tokens3.json"
    tokens.write_text(json.dumps({"access_token": "STALE", "refresh_token": "RT1",
                                  "expires_at": int(time.time()) + 9999}))
    seen = []

    def fake_post(url, data, headers=None):
        seen.append((url, (headers or {}).get("Authorization")))
        if url == auth.TOKEN_URL:
            return {"status": 0, "body": {"access_token": "FRESH",
                                          "refresh_token": "RT2", "expires_in": 10800}}
        if (headers or {}).get("Authorization") == "Bearer STALE":
            return {"status": 343}          # OAuth access token absent or invalid
        return {"status": 0, "body": {"measuregrps": FIXTURE_GROUPS[:1], "more": 0}}

    with stubbed(CREDENTIALS_FILE=creds, TOKENS_FILE=tokens, _post_form=fake_post):
        groups, _tz = withings.get_measure_groups()

    urls = [u for u, _ in seen]
    check("recovered and returned data", len(groups) == 1, str(len(groups)))
    check("refreshed in between", auth.TOKEN_URL in urls, str(urls))
    check("retried with the fresh token",
          seen[-1][1] == "Bearer FRESH", str(seen[-1]))
    check("retried exactly once", len(seen) == 3, str(len(seen)))

    def always_bad(url, data, headers=None):
        if url == auth.TOKEN_URL:
            return {"status": 0, "body": {"access_token": "FRESH",
                                          "refresh_token": "RT3", "expires_in": 10800}}
        return {"status": 343}

    with stubbed(CREDENTIALS_FILE=creds, TOKENS_FILE=tokens, _post_form=always_bad):
        try:
            withings.get_measure_groups()
            check("gives up after one retry", False, "no exception")
        except auth.WithingsAPIError as e:
            check("gives up after one retry", e.status == 343, str(e))


# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _drive_server(state_sent: str, expected_state: str, exchange, port: int):
    """Run the callback server in a thread and hit it with a fake redirect."""
    box = {}

    def run():
        try:
            box["result"] = auth.run_callback_server(
                expected_state, port, "/callback", timeout=10, exchange=exchange)
        except Exception as e:  # noqa: BLE001 - the test inspects it
            box["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while time.time() < deadline:                      # wait for the bind
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                break
        except OSError:
            time.sleep(0.05)

    url = f"http://127.0.0.1:{port}/callback?code=CODE123&state={state_sent}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            box["http_status"] = resp.status
            box["page"] = resp.read().decode()
    except urllib.error.HTTPError as e:
        box["http_status"] = e.code
        box["page"] = e.read().decode()
    thread.join(timeout=10)
    box["alive"] = thread.is_alive()
    return box


def test_callback_server_success() -> None:
    section("local callback server: happy path")
    exchanged = []

    def fake_exchange(code):
        exchanged.append(code)
        return {"access_token": "AT", "refresh_token": "RT", "expires_at": 123,
                "userid": 7, "scope": "user.metrics"}

    port = free_port()
    box = _drive_server("STATE-OK", "STATE-OK", fake_exchange, port)
    check("browser got HTTP 200", box.get("http_status") == 200, str(box.get("http_status")))
    check("page confirms authorization", "Authorized" in box.get("page", ""))
    check("code was exchanged", exchanged == ["CODE123"], str(exchanged))
    check("tokens returned to the caller",
          box.get("result", {}).get("access_token") == "AT", str(box.get("result")))
    check("server thread exited", box["alive"] is False)

    # The closed connection leaves the port in TIME_WAIT, so rebind the way the
    # server itself does (allow_reuse_address) - that is what a re-run must do.
    try:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        check("port rebindable after shutdown (authorize can be re-run)", True)
    except OSError as e:
        check("port rebindable after shutdown (authorize can be re-run)", False, str(e))


def test_callback_server_state_mismatch() -> None:
    section("local callback server: state mismatch is rejected")
    exchanged = []
    port = free_port()
    box = _drive_server("WRONG-STATE", "STATE-OK",
                        lambda code: exchanged.append(code), port)
    check("browser got HTTP 400", box.get("http_status") == 400, str(box.get("http_status")))
    check("page says state mismatch", "State mismatch" in box.get("page", ""),
          box.get("page", "")[:120])
    check("code was NEVER exchanged", exchanged == [], str(exchanged))
    check("caller got WithingsAuthError",
          isinstance(box.get("error"), auth.WithingsAuthError), str(box.get("error")))
    check("server thread exited", box["alive"] is False)


def test_port_in_use() -> None:
    section("local callback server: port already in use")
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            auth.run_callback_server("S", port, "/callback", timeout=1,
                                     exchange=lambda c: {})
            check("raises a clear error", False, "no exception")
        except auth.WithingsAuthError as e:
            check("raises a clear error naming the port",
                  str(port) in str(e) and "--listen" in str(e), str(e))


def test_relay_mode(tmp: Path) -> None:
    section("relay mode: https registered URL, loopback listener")
    creds = tmp / "credentials.json"
    tokens = tmp / "tokens_relay.json"
    creds.write_text(json.dumps({
        "client_id": "CID", "client_secret": "CSEC",
        "redirect_uri": auth.DEFAULT_REDIRECT_URI,
        "listen_uri": auth.DEFAULT_LISTEN_URI,
    }))
    check("registered redirect is https, not loopback",
          auth.DEFAULT_REDIRECT_URI.startswith("https://")
          and not auth.is_loopback(auth.DEFAULT_REDIRECT_URI), auth.DEFAULT_REDIRECT_URI)
    check("listener is loopback", auth.is_loopback(auth.DEFAULT_LISTEN_URI))

    sent = []

    def fake_post(url, data, headers=None):
        sent.append(dict(data))
        return {"status": 0, "body": {"access_token": "AT", "refresh_token": "RT",
                                      "expires_in": 10800, "userid": 9,
                                      "scope": "user.metrics"}}

    port = free_port()
    with stubbed(CREDENTIALS_FILE=creds, TOKENS_FILE=tokens, _post_form=fake_post):
        box = _drive_server("RELAY-STATE", "RELAY-STATE",
                            auth.make_exchanger(auth.DEFAULT_REDIRECT_URI), port)

    check("listener caught the forwarded redirect", box.get("http_status") == 200,
          str(box.get("http_status")))
    check("tokens returned", box.get("result", {}).get("access_token") == "AT",
          str(box.get("result")))
    check("exactly one token call", len(sent) == 1, str(len(sent)))
    check("exchange sent the REGISTERED https redirect_uri",
          bool(sent) and sent[0].get("redirect_uri") == auth.DEFAULT_REDIRECT_URI,
          str(sent[:1]))
    check("exchange did NOT send the loopback listener",
          bool(sent) and sent[0].get("redirect_uri") != auth.DEFAULT_LISTEN_URI)
    check("exchange used the authorization_code grant",
          bool(sent) and sent[0].get("grant_type") == "authorization_code", str(sent[:1]))
    check("code forwarded intact", bool(sent) and sent[0].get("code") == "CODE123",
          str(sent[:1]))
    check("tokens persisted", json.loads(tokens.read_text())["refresh_token"] == "RT")

    # State is still validated on the relayed request.
    sent.clear()
    port2 = free_port()
    with stubbed(CREDENTIALS_FILE=creds, TOKENS_FILE=tokens, _post_form=fake_post):
        box2 = _drive_server("TAMPERED", "RELAY-STATE",
                             auth.make_exchanger(auth.DEFAULT_REDIRECT_URI), port2)
    check("relayed request with a bad state is rejected, nothing exchanged",
          box2.get("http_status") == 400 and sent == [], str(box2.get("http_status")))

    check("stored listen_uri wins over the https redirect",
          auth.resolve_listen_uri(auth.DEFAULT_LISTEN_URI, auth.DEFAULT_REDIRECT_URI)
          == auth.DEFAULT_LISTEN_URI)
    check("no stored listener + https redirect falls back to the default listener",
          auth.resolve_listen_uri(None, auth.DEFAULT_REDIRECT_URI)
          == auth.DEFAULT_LISTEN_URI)
    check("legacy loopback redirect still listens on itself (back-compat)",
          auth.resolve_listen_uri(None, "http://localhost:5000/get_token")
          == "http://localhost:5000/get_token")


def test_relay_page() -> None:
    section("relay page (docs/withings-callback/index.html)")
    docs = auth.SKILL_DIR.parent / "docs"
    page = docs / "withings-callback" / "index.html"
    check("page exists", page.exists(), str(page))
    if not page.exists():
        return
    html = page.read_text()
    check("forwards to the local listener",
          auth.DEFAULT_LISTEN_URI in html, auth.DEFAULT_LISTEN_URI)
    check("uses location.replace", "location.replace" in html)
    check("passes the query string through", "location.search" in html)
    check("shows the exchange fallback command", "auth.py exchange --code" in html)
    check("warns about the 30-second expiry", "30 seconds" in html)
    check("handles an error param", "'error'" in html)
    check("noindex", 'name="robots" content="noindex"' in html)
    check("no-referrer", 'name="referrer" content="no-referrer"' in html)

    urls = [u for u in re.findall(r"https?://[^\s'\"<>)]+", html)
            if not u.startswith("http://localhost")]
    check("no external resources or off-localhost URLs", urls == [], str(urls))
    check("no storage APIs used",
          not any(s in html for s in ("localStorage", "sessionStorage", "document.cookie")))
    check("no network calls from the page",
          not any(s in html for s in ("fetch(", "XMLHttpRequest", "navigator.sendBeacon")))
    check(".nojekyll present", (docs / ".nojekyll").exists())


def test_redirect_uri_parsing() -> None:
    section("redirect URI parsing")
    check("default listener splits to host/port/path",
          auth.split_redirect_uri(auth.DEFAULT_LISTEN_URI)
          == ("localhost", 8765, "/callback"))
    check("https default port",
          auth.split_redirect_uri("https://example.com/cb") == ("example.com", 443, "/cb"))
    check("localhost is loopback", auth.is_loopback("http://localhost:5000/get_token"))
    check("127.0.0.1 is loopback", auth.is_loopback("http://127.0.0.1:8765/callback"))
    check("public host is not loopback", not auth.is_loopback("https://example.com/cb"))
    url = auth.build_authorize_url("CID", auth.DEFAULT_REDIRECT_URI, "ST")
    check("authorize URL carries the required params",
          all(p in url for p in ("response_type=code", "client_id=CID",
                                 "state=ST", "scope=user.metrics")), url)
    check("authorize URL points at account.withings.com",
          url.startswith("https://account.withings.com/oauth2_user/authorize2?"), url)
    check("authorize URL carries the REGISTERED redirect, url-encoded",
          urllib.parse.quote(auth.DEFAULT_REDIRECT_URI, safe="") in url, url)


# ---------------------------------------------------------------------------

def main() -> int:
    print("withings-weight self-test (offline, no credentials)")
    test_unit_scaling()
    test_parsing()
    test_pagination_merge()
    test_paginated_fetch()
    test_first_of_day()
    test_weekly_selection()
    test_wrapped_status()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_token_rotation(tmp)
        test_refresh_failure(tmp)
        test_auto_refresh_retry(tmp)
        test_relay_mode(tmp)
    test_callback_server_success()
    test_callback_server_state_mismatch()
    test_port_in_use()
    test_relay_page()
    test_redirect_uri_parsing()

    print(f"\n{PASSED} passed, {len(FAILURES)} failed")
    for name in FAILURES:
        print(f"  - {name}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
