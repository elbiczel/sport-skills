#!/usr/bin/env python3
"""Shared spec handling for the Hevy write paths.

Both routines and workouts are "a title plus a list of exercises, each with a
list of sets". They differ only in a few per-set and per-exercise fields, so
validation, exercise-name resolution and custom-template creation live here and
are shared by create_routine.py and create_workout.py.
"""

from __future__ import annotations

import hevy
from hevy import _fmt_set, find_exercises, request, resolve_exercise, sync_catalog

SET_TYPES = {"normal", "warmup", "failure", "dropset"}
VALID_RPE = {6, 7, 7.5, 8, 8.5, 9, 9.5, 10}

# Fields common to routine sets and workout sets
BASE_SET_FIELDS = ("weight_kg", "reps", "distance_meters", "duration_seconds", "custom_metric")

MUSCLE_GROUPS = {
    "abdominals", "shoulders", "biceps", "triceps", "forearms", "quadriceps",
    "hamstrings", "calves", "glutes", "abductors", "adductors", "lats",
    "upper_back", "traps", "lower_back", "chest", "cardio", "neck", "full_body", "other",
}
EQUIPMENT_CATEGORIES = {
    "none", "barbell", "dumbbell", "kettlebell", "machine", "plate",
    "resistance_band", "suspension", "other",
}
EXERCISE_TYPES = {
    "weight_reps", "reps_only", "bodyweight_reps", "bodyweight_assisted_reps",
    "duration", "weight_duration", "distance_duration", "short_distance_weight",
}


class SpecError(ValueError):
    """A spec the user can fix - bad enum, missing field, unresolved exercise."""


# ---------------------------------------------------------------- sets


def build_set(raw: dict, where: str, allow_rpe: bool = False, allow_rep_range: bool = False) -> dict:
    """Validate one set. `rpe` is workout-only; `rep_range` is routine-only."""
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: each set must be an object, got {type(raw).__name__}")

    set_type = raw.get("type", "normal")
    if set_type not in SET_TYPES:
        raise SpecError(f"{where}: set type '{set_type}' not in {sorted(SET_TYPES)}")

    out = {"type": set_type}
    for field in BASE_SET_FIELDS:
        if raw.get(field) is not None:
            out[field] = raw[field]

    if raw.get("rpe") is not None:
        if not allow_rpe:
            raise SpecError(f"{where}: rpe is only valid on logged workouts, not routines")
        if raw["rpe"] not in VALID_RPE:
            raise SpecError(f"{where}: rpe {raw['rpe']} invalid - Hevy accepts {sorted(VALID_RPE)}")
        out["rpe"] = raw["rpe"]

    rep_range = raw.get("rep_range")
    if rep_range is not None:
        if not allow_rep_range:
            raise SpecError(f"{where}: rep_range is only valid on routines, not logged workouts")
        if not isinstance(rep_range, dict) or "start" not in rep_range or "end" not in rep_range:
            raise SpecError(f"{where}: rep_range must be an object with `start` and `end`")
        if rep_range["end"] < rep_range["start"]:
            raise SpecError(f"{where}: rep_range end ({rep_range['end']}) is below start")
        out["rep_range"] = {"start": rep_range["start"], "end": rep_range["end"]}
        if "reps" in out:
            raise SpecError(f"{where}: set `reps` or `rep_range`, not both")

    if len(out) == 1:
        raise SpecError(f"{where}: set has no values (need reps/rep_range/weight_kg/duration/distance)")
    return out


def format_set(s: dict) -> str:
    if s.get("rep_range"):
        # feed the range through the normal formatter as the reps token, so it
        # lands in the usual position: "80kg x4-6"
        base = dict(s)
        rr = base.pop("rep_range")
        base["reps"] = f"{rr['start']}-{rr['end']}"
        return _fmt_set(base)
    return _fmt_set(s)


# ---------------------------------------------------------------- exercises


def resolve_exercises(exercises_in: list, set_kwargs: dict, extra_fields: tuple = ()) -> dict:
    """Resolve a spec's exercise list against the predefined template catalog.

    Returns {exercises, resolutions, unresolved}. `exercises` is only usable when
    `unresolved` is empty - nothing is ever sent half-resolved.
    """
    if not exercises_in:
        raise SpecError("spec has no exercises")

    resolutions, unresolved, out = [], [], []

    for i, ex in enumerate(exercises_in):
        where = f"exercises[{i}]"
        template_id = ex.get("exercise_template_id")
        name = ex.get("exercise") or ex.get("title") or ex.get("name")

        if template_id:
            resolutions.append({"query": name or template_id, "status": "explicit_id",
                                "template_id": template_id, "title": name or "(by id)"})
        else:
            if not name:
                raise SpecError(f"{where}: needs `exercise` (a name) or `exercise_template_id`")
            res = resolve_exercise(name)
            if res["status"] != "resolved":
                unresolved.append({
                    "index": i,
                    "query": name,
                    "status": res["status"],
                    "candidates": [
                        {"title": c["template"]["title"], "id": c["template"]["id"],
                         "score": c["score"], "is_custom": bool(c["template"].get("is_custom"))}
                        for c in res["candidates"][:5]
                    ],
                })
                continue
            t = res["template"]
            resolutions.append({
                "query": name,
                "status": "custom_match" if t.get("is_custom") else "resolved",
                "template_id": t["id"],
                "title": t["title"],
                "score": res["candidates"][0]["score"],
            })
            template_id = t["id"]

        sets = [build_set(s, f"{where}.sets[{j}]", **set_kwargs)
                for j, s in enumerate(ex.get("sets") or [])]
        if not sets:
            raise SpecError(f"{where} ('{name}'): no sets")

        entry = {"exercise_template_id": template_id, "sets": sets}
        if ex.get("notes"):
            entry["notes"] = ex["notes"]
        if ex.get("superset_id") is not None:
            entry["superset_id"] = ex["superset_id"]
        for field in extra_fields:
            if ex.get(field) is not None:
                entry[field] = ex[field]
        out.append(entry)

    return {"exercises": out, "resolutions": resolutions, "unresolved": unresolved}


def format_resolutions(plan: dict) -> list:
    """Lines showing how each name mapped onto a template, plus misses."""
    lines = []
    for r in plan["resolutions"]:
        mark = {"resolved": "OK  ", "explicit_id": "ID  ", "custom_match": "CUST"}[r["status"]]
        score = f"  (match {r['score']})" if "score" in r else ""
        lines.append(f"  {mark} '{r['query']}' -> {r['title']}  [{r['template_id']}]{score}")
    for u in plan["unresolved"]:
        lines.append(f"  MISS '{u['query']}' -> {u['status']}")
        for c in u["candidates"]:
            tag = " [custom]" if c["is_custom"] else ""
            lines.append(f"         closest: {c['score']:5.1f}  {c['title']}{tag}")
    return lines


def assert_resolved(plan: dict) -> None:
    if plan["unresolved"]:
        names = ", ".join(f"'{u['query']}'" for u in plan["unresolved"])
        raise SpecError(
            f"Unresolved exercises: {names}. Pick a predefined template (see the candidates), "
            f"pass exercise_template_id explicitly, or create a custom template first."
        )


# ---------------------------------------------------------------- custom templates


def create_custom_exercise(
    title: str,
    exercise_type: str = "weight_reps",
    equipment_category: str = "other",
    muscle_group: str = "other",
    other_muscles: list | None = None,
    confirm: bool = False,
) -> dict:
    """Create a custom exercise template. Only for names with no predefined match.

    Checks the catalog first and refuses if something close already exists -
    duplicating a built-in template splits an exercise's history in Hevy.
    """
    if not title:
        raise SpecError("--title is required")
    if exercise_type not in EXERCISE_TYPES:
        raise SpecError(f"exercise_type must be one of {sorted(EXERCISE_TYPES)}")
    if equipment_category not in EQUIPMENT_CATEGORIES:
        raise SpecError(f"equipment_category must be one of {sorted(EQUIPMENT_CATEGORIES)}")
    if muscle_group not in MUSCLE_GROUPS:
        raise SpecError(f"muscle_group must be one of {sorted(MUSCLE_GROUPS)}")
    for m in other_muscles or []:
        if m not in MUSCLE_GROUPS:
            raise SpecError(f"other_muscles entry '{m}' must be one of {sorted(MUSCLE_GROUPS)}")

    existing = find_exercises(title, limit=3)
    if existing and existing[0]["score"] >= hevy.ACCEPT_SCORE:
        near = ", ".join(f"{m['template']['title']} ({m['score']})" for m in existing)
        raise SpecError(
            f"'{title}' already looks covered by an existing template: {near}. "
            f"Use that instead of creating a duplicate."
        )

    if not confirm:
        raise SpecError("Refusing to create a custom exercise without confirm=True.")

    result = request("POST", "/exercise_templates", body={
        "exercise": {
            "title": title,
            "exercise_type": exercise_type,
            "equipment_category": equipment_category,
            "muscle_group": muscle_group,
            "other_muscles": other_muscles or [],
        }
    })
    sync_catalog(force=True)  # so the new template resolves immediately
    return result
