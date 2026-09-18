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

Even weight is only comparable like-for-like: **the Friday reading taken in the
morning, before coffee, is the comparable one.** An evening reading after a long
run can differ by more than a kilogram from the same morning's, purely on
hydration and gut content. The `weekly` command exists to enforce exactly this -
it picks the first Friday reading of each ISO week and says so when it had to
substitute another day.

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
4. Set the **Callback URL** to exactly:

   ```
   http://localhost:8765/callback
   ```

   Withings' own OAuth sample registers a loopback callback this way
   (`http://localhost:5000/get_token`). If the dashboard rejects a `localhost`
   URL, see *Troubleshooting → the dashboard will not accept localhost*.
5. Fill in the remaining fields (name, description, logo) however you like.
6. Copy the **Client ID** and **Client Secret**.

### 2. Store the credentials

```bash
cd withings-weight/scripts
uv run auth.py init
# Prompts for Client ID and Client Secret.
# Writes withings-weight/data/credentials.json (git-ignored, chmod 600).
```

`--client-id` / `--client-secret` / `--redirect-uri` skip the prompts.
`WITHINGS_CLIENT_ID` and `WITHINGS_CLIENT_SECRET` in the environment override
the stored file.

### 3. Authorize

```bash
uv run auth.py authorize
```

This opens the browser, catches the redirect on `localhost:8765`, and exchanges
the code immediately. **The authorization code is valid for only 30 seconds**,
which is why this is a local-server flow and not a copy-paste one.

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
$ uv run withings.py weekly --weeks 3
week      used               weight   delta     mean  note
--------- ---------------- -------- ------- --------  ----
2026-W36  2026-09-04 Fri      75.10       -    75.50  Friday, 1st of 2 (2 readings)
2026-W37  2026-09-12 Sat      75.40   +0.30    75.43  no Friday reading, used Sat (3 readings)
2026-W38  2026-09-18 Fri      74.80   -0.60    74.80  Friday (1 reading)
```

Rows run **oldest first**, so `delta` is always against the row above. `mean`
averages every reading in that week, not just the chosen one - it moves less
than a single weigh-in and is the better read when a week is noisy. `note` says
which day was used, so a substituted day is never mistaken for a Friday.

### Python API

```python
import sys; sys.path.append('withings-weight/scripts')
from datetime import datetime, timedelta
from withings import (
    list_measurements, latest_measurement, weekly_summary,
    describe_measurement, format_weekly_rows,
)

for r in list_measurements(since=datetime.now() - timedelta(days=28)):
    print(describe_measurement(r))

print(describe_measurement(latest_measurement()))

for line in format_weekly_rows(weekly_summary(weeks=8)):
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
- `data/credentials.json` - Client ID / Secret / callback URL (git-ignored)
- `data/tokens.json` - access + refresh token, expiry, userid (git-ignored)

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
  or re-run `init` with `--redirect-uri http://localhost:<port>/callback` **and
  change the Callback URL on the Withings application to match** - the exchange
  is rejected if the two differ.
- **Status 304 on exchange** - the authorization code expired. It lasts 30
  seconds; just re-run `authorize`.
- **Status 342** - the Client ID or Secret is wrong. Re-run `init`.
- **Status 343** - the access token was rejected. The client refreshes and
  retries once automatically; a second 343 means the grant is gone - re-authorize.
- **Status 601** - rate limited. Withings wants at most one poll per 10 minutes.
- **Status 100-102 / 200 / 401** - authentication failed; treated as a token
  problem and retried once after a refresh.
- **The dashboard will not accept localhost** - Withings' own OAuth sample
  registers `http://localhost:5000/get_token`, and an archived Withings FAQ
  claimed localhost and IP addresses were not allowed as callback URLs. If the
  form refuses it, register a public `https://` URL you control instead, then
  `uv run auth.py init --redirect-uri https://your.domain/callback` and use the
  manual path: `uv run auth.py authorize --manual`, then **within 30 seconds**
  `uv run auth.py exchange --code <CODE>` with the `code` from the redirect.
  `authorize` switches to this mode by itself for any non-loopback callback URL.
- **No weigh-ins returned** - confirm the scale actually synced (check the
  Withings app), and that the app was authorized with `user.metrics`.
- **Testing without a device** - `uv run auth.py authorize --demo` adds
  `mode=demo`, which authorizes Withings' dummy demo account.
- **Verifying the logic offline** - `uv run selftest.py` exercises decoding,
  pagination, first-of-day marking, weekly selection, token rotation and the
  callback server against fixtures. No credentials needed.
