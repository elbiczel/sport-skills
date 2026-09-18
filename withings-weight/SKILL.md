---
name: withings-weight
description: Read body weight and weigh-ins from a Withings scale via the Withings Health API. Use when the user asks about their body weight, weight trend, weigh-ins, body composition, scale data, body fat, lean mass, or wants the weekly weigh-in figure for the training plan. Complements strava-activities (runs and rides) and hevy-workouts (strength sessions).
---

# Withings Weight

Read the user's weigh-ins - date, local time, weight in kg, and whatever body
composition the scale recorded - from the Withings Health API. Running lives in
`strava-activities`, strength in `hevy-workouts`; body mass lives here.

## Weight is the signal; the composition fields are context

The scale estimates body fat by bioelectrical impedance, and that number is
**known to disagree with the user's DEXA scan**. Print the composition fields,
but do not build logic on them, do not trend them, and do not treat a fat-ratio
move as evidence of anything. **Weight is the number to trend.**

Even weight is only comparable like-for-like: **morning readings, before coffee.**
An evening reading after a long run can differ by more than a kilogram from the
same morning's, purely on hydration and gut content.

**The trend figure is the weekly mean of morning readings**, not one weigh-in. A
single reading carries about half a kilo of day-to-day noise from water, salt and
glycogen; averaging a week's mornings removes most of it. `weekly` lists every
morning reading (first of the day, before 11:00 local), their mean, and the
change in that mean against the previous week. It also shows the Friday reading,
which is the fallback comparison when a week has fewer than three mornings.
**Always read and report all of a week's readings, not just one number.**

## One-time setup

Withings requires an OAuth 2.0 developer application. The user creates it once;
tokens are then stored locally and refreshed automatically.

### 1. Create the Withings application

1. Sign in at **https://developer.withings.com/dashboard/** (the "Log In to
   Withings Partner Hub" button on developer.withings.com) with a normal
   Withings account - the same one the scale reports to. The older URL
   `account.withings.com/partner/add_oauth2` now redirects here.
2. Create an application. Choose the **Public API** integration (also called
   *app to app*) - it is the one that needs **no contract** with Withings.
3. Set **Environment** to `dev`.
4. In **Registered URLs**, enter exactly:

   ```
   https://elbiczel.github.io/sport-skills/withings-callback/
   ```

   **The dashboard refuses `localhost` and IP addresses here** (confirmed
   2026-09-18). That URL is a static relay page served by GitHub Pages out of
   this repo (`docs/withings-callback/`): it reads `code` and `state` off its own
   query string and immediately forwards them, unchanged, to the listener that
   `auth.py authorize` runs on `http://localhost:8765/callback`. The code
   therefore passes through the browser and appears in GitHub Pages' request log,
   which is harmless - an authorization code is useless without the client secret
   and expires 30 seconds after it is issued.
5. Fill in the remaining fields (name, description, logo) however you like.
6. Copy the **Client ID** and **Client Secret**.

### 2. Store the credentials

```bash
cd withings-weight/scripts
uv run auth.py init
# Prompts for Client ID and Client Secret.
# Writes withings-weight/data/credentials.json (git-ignored, chmod 600).
```

The defaults are already the relay URL and `http://localhost:8765/callback`, so
plain `uv run auth.py init` is correct - nothing else needs passing.
`--client-id` / `--client-secret` skip the prompts; `--redirect-uri` sets the
registered URL and `--listen` the local address. `WITHINGS_CLIENT_ID` and
`WITHINGS_CLIENT_SECRET` in the environment override the stored file.

Two addresses, deliberately kept apart:

| | value | who sees it |
|---|---|---|
| `redirect_uri` | the https relay page | registered with Withings; sent in the authorize URL **and** echoed back in the token exchange |
| `listen_uri` | `http://localhost:8765/callback` | only this machine; where `authorize` waits |

**Port 8765 is hardcoded in the relay page.** Pointing `--listen` at another port
also means editing `docs/withings-callback/index.html` and pushing it, so the
deployed page keeps matching. (The port is not carried in `state`: that would
couple a separately-deployed static page to the CLI, and a version skew between
them would break authorization silently.)

### 3. Authorize

```bash
uv run auth.py authorize
```

This starts the local listener, opens the browser, lets the relay page bounce the
redirect back to `localhost:8765`, and exchanges the code immediately - sending
Withings the **registered** https URL as `redirect_uri`, not the loopback one.
**The authorization code is valid for only 30 seconds**, which is why this is a
local-server flow and not a copy-paste one.

```bash
uv run auth.py check     # confirms it works, prints the latest weigh-in
```

After this the access token refreshes itself. The user should not need to
authorize again unless they revoke access or the refresh token lapses.

## Reading

```bash
cd withings-weight/scripts

uv run withings.py list                  # last 28 days, one line per weigh-in
uv run withings.py list --days 90
uv run withings.py latest                # most recent weigh-in
uv run withings.py weekly                # last 8 ISO weeks, one row each
uv run withings.py weekly --weeks 12
```

`--json` on any command. `list` and `latest` emit the decoded readings;
`weekly` emits the computed rows.

```
$ uv run withings.py list --days 14
2026-09-18  Fri  06:50   74.80 kg   fat 16.0%  fat mass 11.97 kg  lean 62.83 kg  muscle 59.84 kg  bone 3.12 kg  water 43.38 kg
2026-09-12  Sat  21:00   76.00 kg   fat 17.3%  ...
2026-09-12  Sat  08:10   75.40 kg   fat 16.6%  ...   * first of 2 today
```

When a day holds more than one reading, the earliest is marked `* first of N
today` - that is the comparable one; the others are same-day noise.

```
$ uv run withings.py weekly --weeks 2
week      mornings    mean   delta  Friday  morning readings
--------- -------- ------- ------- -------  ----------------
2026-W38         2   74.95       -   74.78  Wed 75.13 · Fri 74.78   (+2 other, not averaged)
2026-W39         5   74.62   -0.33   74.50  Mon 74.90 · Tue 74.70 · Wed 74.60 · Thu 74.40 · Fri 74.50
```

Rows run **oldest first**, so `delta` is always against the row above. `mean`
is the mean of the morning readings listed on the right; readings later in the
day, and second readings on the same morning, are counted under "other" and left
out. `delta` is the change in that mean. Trust it when both weeks have three or
more mornings; with fewer, compare `Friday` to `Friday`. `--json` also carries
the older single-reading fields (`weight`, `delta`, `mean_weight`, `note`).

### Python API

```python
import sys; sys.path.append('withings-weight/scripts')
from datetime import datetime, timedelta
from withings import (
    list_measurements, latest_measurement, weekly_summary,
    describe_measurement, format_weekly_trend,
)

for r in list_measurements(since=datetime.now() - timedelta(days=28)):
    print(describe_measurement(r))

print(describe_measurement(latest_measurement()))

for line in format_weekly_trend(weekly_summary(weeks=8)):
    print(line)
```

A reading is a flat dict: `date`, `weekday`, `time`, `datetime` (local, aware),
`timestamp`, `weight`, `fat_ratio`, `fat_mass`, `fat_free_mass`, `muscle_mass`,
`bone_mass`, `hydration`, `first_of_day`, `day_count`, `source`, `model`,
`grpid`. Any field the scale did not record is `None`.

Times are rendered in **Europe/Zurich**, falling back to the timezone the API
reports and then to UTC if the system has no tz database.

## Withings API reference

- **Base URL**: `https://wbsapi.withings.net`
- **Auth**: OAuth 2.0, `Authorization: Bearer <access_token>`
- **Everything is `POST` with `application/x-www-form-urlencoded`.**
- **Responses are wrapped**: `{"status": 0, "body": {...}}`. A non-zero `status`
  is an error **even on HTTP 200** - never trust the HTTP code alone.
- **Access token**: 3 hours (`expires_in: 10800`). **Refresh token**: 1 year,
  **and it rotates on every refresh** - the old one dies 8 hours after the new
  one is issued, so the new one must be persisted every time.
- **Authorization code**: 30 seconds.
- **Scope used**: `user.metrics` (others: `user.info`, `user.activity`,
  `user.sleepevents`).
- **Rate limit**: Withings asks for no more than one poll per 10 minutes per
  user. Status `601` is the rate-limit response.
- **Docs**: https://developer.withings.com/api-reference/ ·
  one-file agent reference: https://developer.withings.com/llms.md

| Step | Endpoint |
|------|----------|
| Consent screen | `GET https://account.withings.com/oauth2_user/authorize2` (`response_type=code`, `client_id`, `scope`, `redirect_uri`, `state` - all required) |
| Code → tokens | `POST https://wbsapi.withings.net/v2/oauth2` (`action=requesttoken`, `grant_type=authorization_code`, `client_id`, `client_secret`, `code`, `redirect_uri`) |
| Refresh | `POST https://wbsapi.withings.net/v2/oauth2` (`action=requesttoken`, `grant_type=refresh_token`, `client_id`, `client_secret`, `refresh_token`) |
| Measurements | `POST https://wbsapi.withings.net/measure` (`action=getmeas`, `meastypes`, `category=1`, `startdate`, `enddate`, `offset`) |

`category=1` is real measures; `2` is user-set objectives (goals), which must
not be mixed into the trend.

### Measure types this skill requests

| Code | Field | Unit |
|------|-------|------|
| 1 | `weight` | kg |
| 5 | `fat_free_mass` | kg |
| 6 | `fat_ratio` | **%** |
| 8 | `fat_mass` | kg |
| 76 | `muscle_mass` | kg |
| 77 | `hydration` | **kg** (mass of water, not a percentage) |
| 88 | `bone_mass` | kg |

Other codes exist if ever needed: `4` height, `168`/`169` extracellular /
intracellular water, `170` visceral fat, `226` BMR, `227` metabolic age.

**Decoding a measure**: `real value = value * 10^unit`. So
`{"value": 75100, "unit": -3}` is 75.1 kg. The API never sends floats.

**Pagination**: a response with `more: 1` carries an `offset`; re-call with that
`offset` until `more` is falsy. `get_measure_groups()` does this and drops any
`grpid` repeated across the boundary.

**Measure groups**: each weigh-in is one `measuregrp` with a `date` (unix
seconds), a `grpid`, an `attrib` (0/8 = captured by the device, 1 = device but
ambiguous between users, 2 = entered by hand) and a list of `measures`.

## Files

- `scripts/auth.py` - OAuth flow, local callback server, token storage + rotation
- `scripts/withings.py` - measurement fetching, decoding, weekly summary, CLI
- `scripts/selftest.py` - offline tests; no credentials, no network
- `data/credentials.json` - Client ID / Secret / both callback URLs (git-ignored)
- `data/tokens.json` - access + refresh token, expiry, userid (git-ignored)
- `../docs/withings-callback/index.html` - the registered relay page (GitHub Pages)
- `../docs/.nojekyll` - stops Pages running the files through Jekyll

## Troubleshooting

- **"Missing Withings credentials"** - run `uv run auth.py init`.
- **"No tokens saved"** - run `uv run auth.py authorize`.
- **Refresh fails / "the refresh token may have expired"** - the refresh token
  lapsed (1 year), access was revoked, or a rotation was issued but never saved
  (the old one dies 8 hours later). Re-run `uv run auth.py authorize`. Nothing
  else recovers it - Withings only offers a code-recovery endpoint to contracted
  partners.
- **"State mismatch on the OAuth callback"** - the redirect did not carry the
  state this run generated, so the code was discarded. Usually a stale browser
  tab from an earlier `authorize`. Close it and re-run.
- **"Could not bind 127.0.0.1:8765"** - something else holds the port. Free it,
  or re-run `init --listen http://localhost:<port>/callback` **and edit the port
  in `docs/withings-callback/index.html` and push**, since the deployed relay
  page is what actually does the forwarding. The *registered* URL does not
  change.
- **The browser blocks the https → `http://localhost` hop** - the relay page
  stays on screen and shows the authorization code with the exact command. Run
  `uv run auth.py exchange --code <CODE>` **within 30 seconds**; `authorize` can
  be left running or cancelled, either is fine.
- **The hop is allowed but the browser shows "connection refused"** - the
  listener was not running (`authorize` starts it, so this means it had already
  timed out or was cancelled). The code is still visible in the address bar of
  that error page: copy it out of the URL and run `exchange --code` within the
  30 seconds. `location.replace` leaves no history entry, so Back will not return
  to the relay page.
- **Withings rejects the redirect at exchange time** - the registered URL and the
  `redirect_uri` sent in the token call must be byte-identical, trailing slash
  included. `authorize` sends the registered https URL, never the listener.
- **Status 304 on exchange** - the authorization code expired. It lasts 30
  seconds; just re-run `authorize`.
- **Status 342** - the Client ID or Secret is wrong. Re-run `init`.
- **Status 343** - the access token was rejected. The client refreshes and
  retries once automatically; a second 343 means the grant is gone - re-authorize.
- **Status 601** - rate limited. Withings wants at most one poll per 10 minutes.
- **Status 100-102 / 200 / 401** - authentication failed; treated as a token
  problem and retried once after a refresh.
- **The relay page 404s** - GitHub Pages is not serving `/docs` from `main` yet,
  or the deploy has not finished. Until it does, use
  `uv run auth.py authorize --manual` and `exchange --code` within 30 seconds.
- **Using a different relay or a loopback callback** - `init --redirect-uri`
  accepts any URL. If the URL given is itself loopback, `authorize` listens on it
  directly and no relay is involved. `--manual` always falls back to printing the
  URL and exchanging by hand.
- **No weigh-ins returned** - confirm the scale actually synced (check the
  Withings app), and that the app was authorized with `user.metrics`.
- **Testing without a device** - `uv run auth.py authorize --demo` adds
  `mode=demo`, which authorizes Withings' dummy demo account.
- **Verifying the logic offline** - `uv run selftest.py` exercises decoding,
  pagination, first-of-day marking, weekly selection, token rotation and the
  callback server against fixtures. No credentials needed.
