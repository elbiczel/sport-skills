#!/usr/bin/env python3
"""Create and update Hevy routines - the templates followed in the gym.

A routine is the prescription (exercises, sets, target reps or rep ranges, rest);
a workout is the log of what actually happened. Building the training plan's
strength sessions as routines is the point of this skill's write path.

Guiding rule: every exercise must map onto a **predefined** Hevy template if one
exists. A custom template is only created when the catalog genuinely has no
match, and never implicitly.

Workflow:
    1. `plan <spec.json>`             - resolve names to template IDs, preview. No writes.
    2. `create <spec.json> --confirm` - POST the routine.
    3. `update <id> <spec.json> --confirm` - PUT over an existing routine.
"""

from __future__ import annotations

import json
import sys

from auth import HevyAuthError
from hevy import HevyAPIError, get_routine, list_routine_folders, request
from specs import (
    SpecError,
    assert_resolved,
    create_custom_exercise,
    format_resolutions,
    format_set,
    resolve_exercises,
)

# Routine sets take rep ranges but not RPE (RPE is something you log, not prescribe).
SET_RULES = {"allow_rpe": False, "allow_rep_range": True}
EXERCISE_EXTRAS = ("rest_seconds",)


def build_routine(spec: dict, for_update: bool = False) -> dict:
    """Turn a spec into a POST/PUT body, resolving exercise names to template IDs.

    Returns {payload, resolutions, unresolved}; `payload` is None if anything
    failed to resolve.
    """
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")
    if not spec.get("title"):
        raise SpecError("routine needs a `title`")
    if spec.get("notes"):
        # Hevy routines carry no top-level notes field: the API accepts the key,
        # returns 200, and silently drops it. Fail loudly rather than lose text.
        raise SpecError(
            "Hevy routines have no routine-level `notes` field - the API accepts it "
            "and silently discards it. Put session-level guidance in the first "
            "exercise's `notes` instead; per-exercise notes do persist."
        )

    resolved = resolve_exercises(spec.get("exercises") or [], SET_RULES, EXERCISE_EXTRAS)

    if resolved["unresolved"]:
        return {"payload": None, **{k: resolved[k] for k in ("resolutions", "unresolved")}}

    routine = {"title": spec["title"], "exercises": resolved["exercises"]}
    if not for_update:
        # folder_id is create-only; null means the default "My Routines" folder
        routine["folder_id"] = spec.get("folder_id")

    return {
        "payload": {"routine": routine},
        "resolutions": resolved["resolutions"],
        "unresolved": [],
    }


def format_plan(plan: dict) -> str:
    lines = format_resolutions(plan)
    if plan["payload"]:
        r = plan["payload"]["routine"]
        lines.append("")
        lines.append(f"  {r['title']}")
        if r.get("notes"):
            lines.append(f"  note: {r['notes']}")
        for ex, res in zip(r["exercises"], plan["resolutions"]):
            head = f"    {res['title']}"
            if ex.get("rest_seconds"):
                head += f"  (rest {ex['rest_seconds']}s)"
            if ex.get("superset_id") is not None:
                head += f"  [superset {ex['superset_id']}]"
            lines.append(head)
            if ex.get("notes"):
                lines.append(f"        note: {ex['notes']}")
            for j, s in enumerate(ex["sets"], 1):
                lines.append(f"        {j}. {format_set(s)}")
    return "\n".join(lines)


def _unwrap(payload, key):
    if isinstance(payload, dict):
        inner = payload.get(key, payload)
        if isinstance(inner, list):
            return inner[0] if inner else {}
        return inner
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload or {}


def create_routine(spec: dict, confirm: bool = False) -> dict:
    """POST a new routine. Refuses unless confirmed and fully resolved."""
    plan = build_routine(spec)
    assert_resolved(plan)
    if not confirm:
        raise SpecError("Refusing to write without confirm=True (dry run only).")
    return _unwrap(request("POST", "/routines", body=plan["payload"]), "routine")


def update_routine(routine_id: str, spec: dict, confirm: bool = False) -> dict:
    """PUT over an existing routine.

    This **replaces** title, notes and the whole exercise list - it is not a
    merge. Fetch the routine first if the intent is to change one exercise.
    """
    plan = build_routine(spec, for_update=True)
    assert_resolved(plan)
    if not confirm:
        raise SpecError("Refusing to write without confirm=True (dry run only).")
    return _unwrap(request("PUT", f"/routines/{routine_id}", body=plan["payload"]), "routine")


def create_folder(title: str, confirm: bool = False) -> dict:
    if not title:
        raise SpecError("folder needs a title")
    if not confirm:
        raise SpecError("Refusing to write without confirm=True (dry run only).")
    return _unwrap(
        request("POST", "/routine_folders", body={"routine_folder": {"title": title}}),
        "routine_folder",
    )


def describe_existing(routine_id: str) -> str:
    """Current state of a routine - show this before overwriting it."""
    r = _unwrap(get_routine(routine_id), "routine")
    lines = [f"  {r.get('title')}  [{r.get('id')}]"]
    if r.get("notes"):
        lines.append(f"  note: {r['notes']}")
    for ex in r.get("exercises") or []:
        head = f"    {ex.get('title')}"
        if ex.get("rest_seconds"):
            head += f"  (rest {ex['rest_seconds']}s)"
        lines.append(head)
        for j, s in enumerate(ex.get("sets") or [], 1):
            lines.append(f"        {j}. {format_set(s)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


USAGE = """Usage: uv run create_routine.py <command> [options]

  plan <spec.json>                       Resolve exercises and preview. Never writes.
  create <spec.json> --confirm           Create the routine.
  update <routine_id> <spec.json> --confirm
                                         Replace an existing routine (not a merge).
  show <routine_id>                      Print a routine as it is now.
  folders                                List routine folders.
  folder --title "Marathon Block" --confirm
                                         Create a routine folder.
  custom --title T [--type ...] [--equipment ...] [--muscle ...] [--other m1,m2] --confirm
                                         Create a custom exercise template (last resort).

Spec format:
{
  "title": "Lower Body + Rotational Power (C)",
  "folder_id": null,
  "exercises": [
    {
      "exercise": "Squat (Barbell)",
      "rest_seconds": 120,
      "notes": "optional cue - the only notes Hevy keeps on a routine",
      "sets": [
        {"type": "warmup", "weight_kg": 60, "reps": 5},
        {"weight_kg": 100, "rep_range": {"start": 4, "end": 6}}
      ]
    },
    {"exercise": "Plank", "sets": [{"duration_seconds": 60}]}
  ]
}
`exercise` is matched against predefined Hevy templates; `exercise_template_id`
pins an exact template. Routine sets accept `rep_range` but not `rpe`.
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
            plan = build_routine(_load(rest[0]))
            print(format_plan(plan))
            if plan["unresolved"]:
                print("\nUnresolved exercises - nothing was sent.", file=sys.stderr)
                return 1
            if cmd == "plan" or not confirm:
                print("\n(dry run - pass --confirm to create)")
                return 0
            created = create_routine(_load(rest[0]), confirm=True)
            print(f"\nCreated routine {created.get('id')}  {created.get('title')}")
            return 0

        if cmd == "update":
            routine_id, spec_path = rest[0], rest[1]
            print("CURRENT:")
            print(describe_existing(routine_id))
            plan = build_routine(_load(spec_path), for_update=True)
            print("\nREPLACED BY:")
            print(format_plan(plan))
            if plan["unresolved"]:
                print("\nUnresolved exercises - nothing was sent.", file=sys.stderr)
                return 1
            if not confirm:
                print("\n(dry run - pass --confirm to overwrite; this replaces the whole routine)")
                return 0
            updated = update_routine(routine_id, _load(spec_path), confirm=True)
            print(f"\nUpdated routine {updated.get('id')}  {updated.get('title')}")
            return 0

        if cmd == "show":
            print(describe_existing(rest[0]))
            return 0

        if cmd == "folders":
            folders = list_routine_folders()
            for f in folders:
                print(f"{f.get('id')}  {f.get('title')}")
            if not folders:
                print("(no folders - routines live in the default 'My Routines')")
            return 0

        if cmd == "folder":
            created = create_folder(_flag(rest, "--title"), confirm=confirm)
            print(json.dumps(created, indent=2))
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
