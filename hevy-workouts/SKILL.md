---
name: hevy-workouts
description: Read strength training from Hevy and build Hevy routines via the Hevy public API. Use when the user asks about their lifting sessions, gym volume, sets/reps/loads, strength progression, or wants a strength session turned into a Hevy routine. Complements strava-activities, which covers runs and rides.
---

# Hevy Workouts

Read the user's logged strength training from Hevy, and write the plan's strength
sessions into Hevy as **routines**. Strength data lives here; running/cycling
lives in `strava-activities`.

## Routines vs workouts

- A **routine** is the prescription — exercises, sets, target reps or rep ranges,
  rest times. It's what the user opens and follows in the gym. **This is the
  thing to create.** `create_routine.py`.
- A **workout** is the log of a session that happened, with the loads and RPE
  actually hit. Hevy records these when the user trains. `create_workout.py`
  exists only to back-fill a session that was done but never logged — don't reach
  for it unless explicitly asked.

Set fields differ accordingly and are enforced: routine sets take `rep_range` but
reject `rpe` (you don't prescribe RPE here); workout sets take `rpe` but reject
`rep_range`.

## The one rule for writing

**Always map an exercise onto a predefined Hevy template. Only create a custom
exercise template when the catalog genuinely has no match — and never silently.**

Hevy ships ~450 built-in templates (`Squat (Barbell)`, `Romanian Deadlift (Barbell)`,
`Standing Calf Raise`, …). A duplicate custom template splits that exercise's
history: PRs, charts and progression in the app stop seeing earlier sets. So the
write path is deliberately two-stage — resolve first, write second:

1. `plan <spec.json>` resolves every exercise name against the cached catalog and
   prints what it matched. It never writes.
2. If anything comes back `ambiguous` or `not_found`, **ask the user** which
   template to use rather than guessing or inventing one.
3. `create <spec.json> --confirm` posts it.

`create` refuses outright if any exercise is unresolved, and `custom` refuses if
a close template already exists. Both refuse without `--confirm`.

Writing touches the user's real Hevy account. Show the `plan` output and get an
explicit go-ahead before passing `--confirm`.

## One-time setup

```bash
cd hevy-workouts/scripts
uv run auth.py init      # paste the API key; writes data/credentials.json (chmod 600)
uv run auth.py check     # verifies the key
uv run hevy.py sync      # caches the exercise template catalog
```

The key comes from **https://hevy.com/settings?developer** and requires **Hevy Pro**.
`HEVY_API_KEY` in the environment overrides the stored file.

## Reading

```bash
cd hevy-workouts/scripts

uv run hevy.py list --limit 10          # recent workouts, one line each
uv run hevy.py list --days 28           # everything in the last 4 weeks
uv run hevy.py recent --days 14         # same, but full exercise/set detail
uv run hevy.py show <workout_id>        # one workout in detail
uv run hevy.py history "back squat"     # every logged set of one exercise over time
uv run hevy.py routines                 # routines on the account
uv run hevy.py me
```

`--json` on any command gives the raw API response.

```python
import sys; sys.path.append('hevy-workouts/scripts')
from datetime import datetime, timedelta, timezone
from hevy import list_workouts, describe_workout, workout_volume, exercise_history, resolve_exercise

since = datetime.now(timezone.utc) - timedelta(days=28)
for w in list_workouts(since=since, max_workouts=200):
    print(describe_workout(w))

sq = resolve_exercise("back squat")["template"]
sets = exercise_history(sq["id"], start_date=since)
```

`workout_volume()` / `exercise_volume()` sum kg × reps over working sets only
(warmups excluded) — that's the number to trend week over week against running load.

## Exercise resolution

```bash
uv run hevy.py search "squat"      # scored candidates from the catalog
uv run hevy.py resolve "back squat"  # resolved / ambiguous / not_found
uv run hevy.py sync                # refresh the catalog (auto-refreshes after 30 days)
```

Matching is token-based, so `barbell squat`, `Squat (Barbell)` and `squat barbell`
all land on the same template. Scores are 0–100; a match needs ≥80 **and** an
8-point gap over the runner-up to resolve on its own. Bare `squat` is correctly
ambiguous (barbell vs dumbbell vs front) — that's a question for the user, not a
coin flip. Built-in templates outrank custom ones at equal score.

## Creating a routine (the main write path)

Spec file:

```json
{
  "title": "Lower Body + Rotational Power (C)",
  "folder_id": null,
  "exercises": [
    {
      "exercise": "Squat (Barbell)",
      "rest_seconds": 120,
      "notes": "optional cue — the only notes a routine keeps",
      "sets": [
        {"type": "warmup", "weight_kg": 40, "reps": 5},
        {"weight_kg": 80, "rep_range": {"start": 4, "end": 6}},
        {"weight_kg": 80, "rep_range": {"start": 4, "end": 6}}
      ]
    },
    {"exercise": "Plank", "sets": [{"duration_seconds": 60}]}
  ]
}
```

- `exercise` is a name to resolve; `exercise_template_id` pins an exact template instead.
- `folder_id`: `null` (or omitted) puts it in the default "My Routines".
- Per exercise: `rest_seconds`, `notes`, `superset_id` (an integer shared across a superset).
- Set fields: `type` (`normal` default, `warmup`, `failure`, `dropset`), `weight_kg`,
  `reps` **or** `rep_range` (not both), `distance_meters`, `duration_seconds`, `custom_metric`.

**There is no routine-level `notes` field.** A routine carries only `title`,
`folder_id` and its exercises — the API accepts a top-level `notes`, returns 200,
and silently discards it. `build_routine` now rejects it rather than losing the
text. Put session-level guidance in the **first exercise's** `notes`, which is
what the user reads when they open the routine.

```bash
uv run create_routine.py plan   /tmp/routine.json              # preview, no writes
uv run create_routine.py create /tmp/routine.json --confirm    # create it
uv run create_routine.py show   <routine_id>                   # current state
uv run create_routine.py update <routine_id> /tmp/routine.json --confirm
uv run create_routine.py folders
uv run create_routine.py folder --title "Marathon Block" --confirm
```

**`update` is a replace, not a merge** — the PUT overwrites title, notes and the
entire exercise list. To change one exercise, `show` the routine first and send
back the full list with that one edit. The dry run prints CURRENT and REPLACED BY
side by side; read both before confirming.

```python
from create_routine import build_routine, format_plan, create_routine
plan = build_routine(spec)
print(format_plan(plan))          # inspect before writing
create_routine(spec, confirm=True)
```

## Logging a completed workout (secondary)

Only when asked to back-fill a session that happened but wasn't logged.

```json
{
  "title": "Lower body strength",
  "start_time": "2026-07-30T18:00:00Z",
  "duration_minutes": 55,
  "exercises": [
    {"exercise": "Squat (Barbell)",
     "sets": [{"type": "warmup", "weight_kg": 60, "reps": 5},
              {"weight_kg": 100, "reps": 5, "rpe": 8}]}
  ]
}
```

- `start_time` is required, plus either `end_time` or `duration_minutes`.
- `rpe` must be one of 6, 7, 7.5, 8, 8.5, 9, 9.5, 10 — Hevy rejects anything else.

```bash
uv run create_workout.py plan /tmp/session.json               # preview, no writes
uv run create_workout.py create /tmp/session.json --confirm
```

## Creating a custom exercise (last resort)

Only after `resolve` returns `not_found` and the user confirms nothing built-in fits:

```bash
uv run create_routine.py custom --title "Sled Push" \
  --type weight_reps --equipment other --muscle quadriceps --other glutes,calves --confirm
```

- `--type`: `weight_reps`, `reps_only`, `bodyweight_reps`, `bodyweight_assisted_reps`,
  `duration`, `weight_duration`, `distance_duration`, `short_distance_weight`
- `--equipment`: `none`, `barbell`, `dumbbell`, `kettlebell`, `machine`, `plate`,
  `resistance_band`, `suspension`, `other`
- `--muscle` / `--other`: `abdominals`, `shoulders`, `biceps`, `triceps`, `forearms`,
  `quadriceps`, `hamstrings`, `calves`, `glutes`, `abductors`, `adductors`, `lats`,
  `upper_back`, `traps`, `lower_back`, `chest`, `cardio`, `neck`, `full_body`, `other`

The catalog re-syncs automatically after a custom template is created, so it
resolves on the next call.

## Hevy API reference

- **Base URL**: `https://api.hevyapp.com/v1`
- **Auth**: `api-key: <key>` header (no OAuth, no expiry). Hevy Pro only.
- **Docs**: https://api.hevyapp.com/docs/
- Page size is capped at **10** for workouts/routines/events, **100** for exercise templates.
- The API is explicitly labelled unstable by Hevy — endpoints may change.

| Function                   | Endpoint                                 |
|----------------------------|------------------------------------------|
| `list_routines()`          | `GET /routines`                          |
| `get_routine(id)`          | `GET /routines/{id}`                     |
| `create_routine()`         | `POST /routines`                         |
| `update_routine(id)`       | `PUT /routines/{id}` (full replace)      |
| `list_routine_folders()`   | `GET /routine_folders`                   |
| `create_folder()`          | `POST /routine_folders`                  |
| `list_workouts()`          | `GET /workouts` (paged)                  |
| `get_workout(id)`          | `GET /workouts/{id}`                     |
| `workout_events(since)`    | `GET /workouts/events`                   |
| `create_workout()`         | `POST /workouts`                         |
| `exercise_history(id)`     | `GET /exercise_history/{id}`             |
| `sync_catalog()`           | `GET /exercise_templates` (paged, 100)   |
| `create_custom_exercise()` | `POST /exercise_templates`               |
| `list_body_measurements()` | `GET /body_measurements`                 |

There is no delete endpoint for routines or workouts — anything created in error
must be removed in the Hevy app by hand. Get the plan right before confirming.
`POST /routines` can also return **403 Routine limit exceeded** on a free account.

## Files

- `scripts/auth.py` — API key storage and validation
- `scripts/hevy.py` — HTTP client, reads, exercise catalog + name matching, CLI
- `scripts/specs.py` — shared spec validation, exercise resolution, custom templates
- `scripts/create_routine.py` — routine create/update/folders, CLI (main write path)
- `scripts/create_workout.py` — logging a completed workout, CLI (secondary)
- `data/credentials.json` — API key (git-ignored)
- `data/exercise_templates.json` — cached catalog (git-ignored)

## Troubleshooting

- **"Missing Hevy API key"** — run `uv run auth.py init`.
- **HTTP 401 / 403** — key is wrong, revoked, or the account is no longer Pro.
- **HTTP 400 on create** — usually an invalid `rpe`, a set with no values, or a set
  whose fields don't match the template type (e.g. `reps` on a `duration` exercise).
- **HTTP 403 on `POST /routines`** — routine limit exceeded on the account.
- **A resolved exercise looks wrong** — run `search` and pass `exercise_template_id`
  explicitly rather than loosening the matcher.
- **New Hevy exercise not found** — `uv run hevy.py sync`.
