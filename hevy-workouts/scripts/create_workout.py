#!/usr/bin/env python3
"""Create logged Hevy workouts - a record of a session that already happened.

Secondary to create_routine.py: routines are the prescription and the thing this
skill normally writes. Use this only when explicitly asked to back-fill a session
that was done but never logged in the app.

Same guiding rule as routines: every exercise must map onto a predefined Hevy
template if one exists; custom templates only as a last resort, never implicitly.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta

from auth import HevyAuthError
from hevy import HevyAPIError, _iso, parse_time, request
from specs import (
    SpecError,
    assert_resolved,
    create_custom_exercise,
    format_resolutions,
    format_set,
    resolve_exercises,
)

# Workout sets carry the RPE actually experienced; rep ranges are a routine concept.
SET_RULES = {"allow_rpe": True, "allow_rep_range": False}


def _resolve_times(spec: dict):
    start = parse_time(spec.get("start_time"))
    if start is None:
        raise SpecError("start_time is required (ISO 8601, e.g. 2026-07-30T18:00:00Z)")

    end = parse_time(spec.get("end_time"))
    if end is None:
        minutes = spec.get("duration_minutes")
        if minutes is None:
            raise SpecError("provide either end_time or duration_minutes")
        end = start + timedelta(minutes=float(minutes))

    if end <= start:
        raise SpecError("end_time must be after start_time")
    return _iso(start), _iso(end)


def build_workout(spec: dict) -> dict:
    """Turn a spec into a POST body, resolving exercise names to template IDs."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")

    start_time, end_time = _resolve_times(spec)
    resolved = resolve_exercises(spec.get("exercises") or [], SET_RULES)

    if resolved["unresolved"]:
        return {"payload": None, **{k: resolved[k] for k in ("resolutions", "unresolved")}}

    workout = {
        "title": spec.get("title") or "Workout",
        "start_time": start_time,
        "end_time": end_time,
        "is_private": bool(spec.get("is_private", False)),
        "exercises": resolved["exercises"],
    }
    if spec.get("description"):
        workout["description"] = spec["description"]

    return {"payload": {"workout": workout}, "resolutions": resolved["resolutions"], "unresolved": []}


def format_plan(plan: dict) -> str:
    lines = format_resolutions(plan)
    if plan["payload"]:
        w = plan["payload"]["workout"]
        lines.append("")
        lines.append(f"  {w['title']}  {w['start_time']} -> {w['end_time']}"
                     f"{'  (private)' if w['is_private'] else ''}")
        for ex, res in zip(w["exercises"], plan["resolutions"]):
            lines.append(f"    {res['title']}")
            for j, s in enumerate(ex["sets"], 1):
                lines.append(f"      {j}. {format_set(s)}")
    return "\n".join(lines)


def create_workout(spec: dict, confirm: bool = False) -> dict:
    """POST a workout. Refuses unless confirmed and fully resolved."""
    plan = build_workout(spec)
    assert_resolved(plan)
    if not confirm:
        raise SpecError("Refusing to write without confirm=True (dry run only).")
    return request("POST", "/workouts", body=plan["payload"])


# ---------------------------------------------------------------- CLI


USAGE = """Usage: uv run create_workout.py <command> [options]

Logs a completed session. To build a routine (the usual case), use create_routine.py.

  plan <spec.json>              Resolve exercises and preview. Never writes.
  create <spec.json> --confirm  Log the workout (omit --confirm for a dry run).
  custom --title T [...] --confirm
                                Create a custom exercise template (last resort).

Spec format:
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
Workout sets accept `rpe` but not `rep_range`.
"""


def _flag(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _cli(argv):
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0

    cmd, rest = argv[0], argv[1:]
    confirm = "--confirm" in rest

    try:
        if cmd in {"plan", "create"}:
            plan = build_workout(_load(rest[0]))
            print(format_plan(plan))
            if plan["unresolved"]:
                print("\nUnresolved exercises - nothing was sent.", file=sys.stderr)
                return 1
            if cmd == "plan" or not confirm:
                print("\n(dry run - pass --confirm to create)")
                return 0
            result = create_workout(_load(rest[0]), confirm=True)
            w = (result or {}).get("workout", result)
            print(f"\nCreated workout {w.get('id') if isinstance(w, dict) else w}")
            return 0

        if cmd == "custom":
            others = _flag(rest, "--other")
            result = create_custom_exercise(
                title=_flag(rest, "--title"),
                exercise_type=_flag(rest, "--type", "weight_reps"),
                equipment_category=_flag(rest, "--equipment", "other"),
                muscle_group=_flag(rest, "--muscle", "other"),
                other_muscles=[m.strip() for m in others.split(",")] if others else None,
                confirm=confirm,
            )
            print(json.dumps(result, indent=2))
            return 0

        print(f"Unknown command: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 1

    except (SpecError, HevyAPIError, HevyAuthError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except IndexError:
        print(f"Missing argument.\n\n{USAGE}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
