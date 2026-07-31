---
name: strava-activities
description: Read Strava activities for the authenticated athlete via the Strava v3 REST API. Use when the user asks about their runs, rides, workouts, weekly mileage, or any question that requires pulling data from their Strava account.
---

# Strava Activities

Fetch and summarize the user's Strava activities via the official Strava v3 REST API.

## Always pull per-split pace/HR

The list endpoint (`/athlete/activities`) returns only whole-activity totals
(average pace, average HR). **Per-split (per-km) pace and HR live only on the
detailed activity** (`GET /activities/{id}`, field `splits_metric`). Whenever a
run/ride matters to the user's question — analysing a workout, checking whether
an easy run stayed aerobic, reading pace/HR drift across a long run — fetch the
detail and report the per-split breakdown alongside the effort totals, not just
the averages. Use `describe_activity_with_splits()` / the `splits` CLI command,
or `format_splits()` if you only want the split lines. Whole-activity averages
hide negative splits, HR drift, and interval structure, so don't stop at them.

## One-time setup

The Strava API requires OAuth 2.0. Before this skill can fetch anything, the user must create a Strava API application and run the auth flow once. Tokens are then stored locally and auto-refreshed.

### 1. Create a Strava API application

1. Go to https://www.strava.com/settings/api
2. Create an app (any name/website is fine for personal use)
3. Set **Authorization Callback Domain** to `localhost`
4. Copy the **Client ID** and **Client Secret**

### 2. Save credentials

```bash
cd strava-activities/scripts
uv run auth.py init
# Prompts for Client ID and Client Secret.
# Writes strava-activities/data/credentials.json (git-ignored).
```

### 3. Authorize and exchange the code

```bash
uv run auth.py authorize
# Prints a URL. Open it in a browser, click Authorize.
# You'll be redirected to http://localhost/exchange_token?code=<CODE>&scope=...
# The page will fail to load - that's expected. Copy the `code` query param.

uv run auth.py exchange
# Paste the code. Tokens are saved to strava-activities/data/tokens.json.
```

After this, `fetch_activities.py` will auto-refresh the access token whenever it's about to expire, so you shouldn't need to re-run the auth flow again unless you revoke access.

## Usage

### Python API

```python
import sys
sys.path.append('strava-activities/scripts')

from fetch_activities import (
    list_activities,
    iter_activities,
    get_activity,
    get_athlete,
    summarize_activity,
    format_splits,
    describe_activity_with_splits,
)

# Most recent 10 activities
for a in list_activities(per_page=10):
    print(summarize_activity(a))

# Stream all activities since Jan 1, 2025
from datetime import datetime
for a in iter_activities(after=datetime(2025, 1, 1), max_activities=500):
    print(a["name"], a["distance"])

# Full detail for one activity (carries splits_metric)
detail = get_activity(1234567890)

# Totals + per-km pace/HR breakdown for one activity
print(describe_activity_with_splits(detail))

# Just the per-split lines
for line in format_splits(detail):
    print(line)

# Authenticated athlete profile
me = get_athlete()
```

### CLI

All scripts are stdlib-only, so `uv run` works without any project config or dependencies.

```bash
cd strava-activities/scripts

# Recent activities (summary lines)
uv run fetch_activities.py list --per-page 10

# Recent activities as raw JSON
uv run fetch_activities.py list --per-page 5 --json

# Full JSON for a single activity by id
uv run fetch_activities.py show 1234567890

# Per-km split pace/HR for one activity (totals line + indented splits)
uv run fetch_activities.py splits 1234567890

# Raw splits_metric JSON for one activity
uv run fetch_activities.py splits 1234567890 --json

# Athlete profile
uv run fetch_activities.py me
```

## Strava API reference

- **Base URL**: `https://www.strava.com/api/v3`
- **Auth**: OAuth 2.0, `Authorization: Bearer <access_token>` header
- **Access token lifetime**: 6 hours; refreshed automatically via the refresh token
- **Rate limits**: 200 requests per 15 min, 2000 per day (per application)
- **Scopes used by this skill**: `read,activity:read_all,profile:read_all` (read all activities including private ones, plus full athlete profile fields like `weight`)
- **Docs**: https://developers.strava.com/docs/reference/

### Main endpoints used

| Function in this skill  | Endpoint                           | Notes                                |
|-------------------------|------------------------------------|--------------------------------------|
| `get_athlete()`         | `GET /athlete`                     | Authenticated athlete profile        |
| `list_activities()`     | `GET /athlete/activities`          | Paged list (`page`, `per_page`, `before`, `after`) |
| `iter_activities()`     | `GET /athlete/activities` (paged)  | Yields across pages until empty      |
| `get_activity(id)`      | `GET /activities/{id}`             | Full detail for one activity         |

### Key fields on a summary activity

`id`, `name`, `sport_type`, `type`, `distance` (meters), `moving_time` (s), `elapsed_time` (s), `total_elevation_gain` (m), `start_date`, `start_date_local`, `timezone`, `average_speed`, `max_speed`, `average_heartrate`, `max_heartrate`, `average_watts`, `kilojoules`, `suffer_score`, `kudos_count`, `map.summary_polyline`.

### Per-split fields (detailed activity only)

`get_activity(id)` adds `splits_metric` (per-km) and `splits_standard` (per-mile), each a list of split objects with: `split` (index), `distance` (m), `moving_time` (s), `elapsed_time` (s), `average_speed` (m/s), `average_heartrate` (bpm), `elevation_difference` (m), `pace_zone`. `laps` is also present and is more meaningful than `splits_metric` for structured interval workouts (each lap = a press of the watch lap button). These fields are **absent** on the summary objects from `list_activities()`.

## Files

- `scripts/auth.py` - OAuth flow, credential/token storage, auto-refresh
- `scripts/fetch_activities.py` - Activity fetching + CLI
- `data/credentials.json` - Client ID / Client Secret (git-ignored)
- `data/tokens.json` - Access + refresh token, expiry (git-ignored)

## Troubleshooting

- **"Missing Strava credentials"** - run `uv run auth.py init`.
- **"No refresh_token saved"** - run `authorize` then `exchange`.
- **HTTP 401 Unauthorized** - the refresh token was revoked. Re-run the authorize + exchange flow.
- **HTTP 429 Too Many Requests** - rate limit hit. Wait 15 minutes, or reduce `per_page` / use `iter_activities(max_activities=...)`.
- **Private activities missing** - the app must have been authorized with the `activity:read_all` scope (the default here).
