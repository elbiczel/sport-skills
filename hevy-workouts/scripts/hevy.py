#!/usr/bin/env python3
"""Hevy API client: reads (workouts, routines, exercise history, body measurements)
plus the local exercise-template catalog used to resolve exercise names to IDs.

Stdlib only - `uv run hevy.py ...` works with no project config.
Writes (creating workouts / custom exercises) live in create_workout.py.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auth import API_BASE, DATA_DIR, HevyAuthError, auth_headers

CATALOG_FILE = DATA_DIR / "exercise_templates.json"
CATALOG_MAX_AGE_DAYS = 30


class HevyAPIError(RuntimeError):
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


# ---------------------------------------------------------------- HTTP


def request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    """Single HTTP call against the Hevy API. Returns parsed JSON (or None on 204)."""
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers=auth_headers(json_body=body is not None)
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 429 and attempt < 2:
                time.sleep(2 ** attempt * 5)
                continue
            raise HevyAPIError(e.code, f"HTTP {e.code} on {method} {path}: {detail}", detail) from e
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise HevyAPIError(None, f"Network error on {method} {path}: {e.reason}") from e


def get(path, params=None):
    return request("GET", path, params=params)


def _paginate(path: str, key: str, page_size: int, max_items: int | None = None):
    """Yield items across pages until exhausted or max_items reached."""
    page = 1
    seen = 0
    while True:
        payload = get(path, {"page": page, "pageSize": page_size}) or {}
        items = payload.get(key) or []
        for item in items:
            yield item
            seen += 1
            if max_items is not None and seen >= max_items:
                return
        page_count = payload.get("page_count")
        if not items or (page_count is not None and page >= page_count):
            return
        page += 1


# ---------------------------------------------------------------- reads


def get_user_info():
    """Account profile. The API wraps it in a `data` envelope; unwrap it."""
    payload = get("/user/info") or {}
    return payload.get("data", payload)


def workout_count() -> int:
    payload = get("/workouts/count") or {}
    return payload.get("workout_count", payload.get("count", 0))


def iter_workouts(max_workouts: int | None = None):
    """Newest-first stream of full workout objects (page size capped at 10 by the API)."""
    return _paginate("/workouts", "workouts", page_size=10, max_items=max_workouts)


def list_workouts(since: datetime | None = None, max_workouts: int = 50) -> list:
    """Workouts newest-first, optionally stopping once older than `since`."""
    out = []
    for w in iter_workouts(max_workouts=max_workouts):
        if since is not None and parse_time(w.get("start_time")) is not None:
            if parse_time(w["start_time"]) < since:
                break
        out.append(w)
    return out


def get_workout(workout_id: str):
    return get(f"/workouts/{workout_id}")


def workout_events(since: datetime | str, max_events: int = 100):
    """Updates/deletes since a timestamp - for keeping a local cache fresh."""
    since_s = since if isinstance(since, str) else since.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    out, page = [], 1
    while len(out) < max_events:
        payload = get("/workouts/events", {"page": page, "pageSize": 10, "since": since_s}) or {}
        events = payload.get("events") or []
        out.extend(events)
        page_count = payload.get("page_count")
        if not events or (page_count is not None and page >= page_count):
            break
        page += 1
    return out[:max_events]


def list_routines(max_routines: int = 50):
    return list(_paginate("/routines", "routines", page_size=10, max_items=max_routines))


def get_routine(routine_id: str):
    return get(f"/routines/{routine_id}")


def list_routine_folders(max_folders: int = 50):
    return list(_paginate("/routine_folders", "routine_folders", page_size=10, max_items=max_folders))


def exercise_history(template_id: str, start_date=None, end_date=None):
    """All logged sets for one exercise template, optionally date-bounded."""
    params = {}
    if start_date:
        params["start_date"] = _iso(start_date)
    if end_date:
        params["end_date"] = _iso(end_date)
    payload = get(f"/exercise_history/{template_id}", params) or {}
    return payload.get("exercise_history", payload.get("events", payload))


def list_body_measurements(max_items: int = 50):
    return list(
        _paginate("/body_measurements", "body_measurements", page_size=10, max_items=max_items)
    )


# ---------------------------------------------------------------- exercise catalog


def sync_catalog(force: bool = False) -> list:
    """Download every exercise template on the account and cache it locally.

    The catalog holds Hevy's ~400 built-in templates plus any custom ones on the
    account. It is the lookup table for turning an exercise name into the
    `exercise_template_id` that workout creation requires.
    """
    if not force and CATALOG_FILE.exists():
        return load_catalog()

    templates = list(_paginate("/exercise_templates", "exercise_templates", page_size=100))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "count": len(templates),
                "templates": templates,
            },
            indent=2,
        )
    )
    return templates


def load_catalog(auto_sync: bool = True) -> list:
    """Cached templates, refetching if the cache is missing or stale."""
    if not CATALOG_FILE.exists():
        if not auto_sync:
            raise HevyAPIError(None, "No exercise catalog cached. Run `uv run hevy.py sync`.")
        return sync_catalog(force=True)

    cache = json.loads(CATALOG_FILE.read_text())
    fetched = parse_time(cache.get("fetched_at"))
    stale = fetched is None or (
        datetime.now(timezone.utc) - fetched > timedelta(days=CATALOG_MAX_AGE_DAYS)
    )
    if stale and auto_sync:
        return sync_catalog(force=True)
    return cache.get("templates", [])


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set:
    return set(_normalize(text).split())


def score_match(query: str, template: dict) -> float:
    """0-100 similarity between a free-text exercise name and a template title.

    Hevy titles carry the equipment in parentheses ("Bench Press (Barbell)"), so
    token-subset matching handles "bench press barbell", "barbell bench press",
    and "Bench Press (Barbell)" alike.
    """
    q_norm, t_norm = _normalize(query), _normalize(template.get("title", ""))
    if not q_norm or not t_norm:
        return 0.0

    if q_norm == t_norm:
        return 100.0

    q_tok, t_tok = set(q_norm.split()), set(t_norm.split())
    ratio = difflib.SequenceMatcher(None, q_norm, t_norm).ratio()

    if q_tok == t_tok:
        return 98.0
    if q_tok <= t_tok:
        # every query word appears in the title; penalise unmatched title words
        extra = len(t_tok - q_tok)
        return max(80.0, 95.0 - 5.0 * extra)
    if t_tok <= q_tok:
        extra = len(q_tok - t_tok)
        return max(72.0, 88.0 - 6.0 * extra)

    overlap = len(q_tok & t_tok) / max(len(q_tok), 1)
    return max(ratio * 70.0, overlap * 68.0)


def find_exercises(query: str, limit: int = 8, include_custom: bool = True) -> list:
    """Best-matching templates for a name, each as {template, score}, best first."""
    catalog = load_catalog()
    scored = []
    for t in catalog:
        if not include_custom and t.get("is_custom"):
            continue
        s = score_match(query, t)
        if s > 0:
            scored.append({"template": t, "score": round(s, 1)})
    # prefer Hevy's built-in templates over custom ones at equal score
    scored.sort(key=lambda r: (-r["score"], bool(r["template"].get("is_custom")), r["template"]["title"]))
    return scored[:limit]


ACCEPT_SCORE = 80.0
AMBIGUOUS_GAP = 8.0


def resolve_exercise(query: str) -> dict:
    """Map an exercise name onto one predefined template.

    Returns {status, template, candidates}:
      - "resolved"  - confident single match; use template["id"]
      - "ambiguous" - several close matches; ask the user which one
      - "not_found" - nothing close; a custom exercise may be needed
    """
    matches = find_exercises(query)
    if not matches:
        return {"status": "not_found", "template": None, "candidates": []}

    best = matches[0]
    runner_up = matches[1]["score"] if len(matches) > 1 else 0.0

    if best["score"] >= ACCEPT_SCORE and (best["score"] - runner_up) >= AMBIGUOUS_GAP:
        return {"status": "resolved", "template": best["template"], "candidates": matches}
    if best["score"] >= ACCEPT_SCORE:
        return {"status": "ambiguous", "template": None, "candidates": matches}
    return {"status": "not_found", "template": None, "candidates": matches}


# ---------------------------------------------------------------- formatting


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _fmt_set(s: dict) -> str:
    bits = []
    if s.get("weight_kg") is not None:
        bits.append(f"{_num(s['weight_kg'])}kg")
    if s.get("reps") is not None:
        bits.append(f"x{_num(s['reps'])}")
    if s.get("distance_meters") is not None:
        bits.append(f"{_num(s['distance_meters'])}m")
    if s.get("duration_seconds") is not None:
        bits.append(f"{_num(s['duration_seconds'])}s")
    if s.get("custom_metric") is not None:
        bits.append(f"metric {_num(s['custom_metric'])}")
    line = " ".join(bits) or "-"
    if s.get("rpe") is not None:
        line += f" @RPE{_num(s['rpe'])}"
    if s.get("type") and s["type"] != "normal":
        line += f" [{s['type']}]"
    return line


def _num(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def exercise_volume(ex: dict) -> float:
    """Working-set tonnage (kg x reps), warmups excluded."""
    total = 0.0
    for s in ex.get("sets") or []:
        if s.get("type") == "warmup":
            continue
        w, r = s.get("weight_kg"), s.get("reps")
        if w and r:
            total += w * r
    return total


def workout_volume(w: dict) -> float:
    return sum(exercise_volume(ex) for ex in w.get("exercises") or [])


def summarize_workout(w: dict) -> str:
    start = parse_time(w.get("start_time"))
    end = parse_time(w.get("end_time"))
    when = start.astimezone().strftime("%Y-%m-%d %H:%M") if start else "?"
    mins = int((end - start).total_seconds() // 60) if start and end else None
    sets = sum(len(ex.get("sets") or []) for ex in w.get("exercises") or [])
    vol = workout_volume(w)
    parts = [
        when,
        w.get("title", "(untitled)"),
        f"{len(w.get('exercises') or [])} exercises",
        f"{sets} sets",
    ]
    if mins is not None:
        parts.append(f"{mins} min")
    if vol:
        parts.append(f"{vol:,.0f} kg volume")
    return " | ".join(parts)


def describe_workout(w: dict) -> str:
    """Summary line plus every exercise and set - the detail view."""
    lines = [summarize_workout(w), f"  id: {w.get('id')}"]
    if w.get("description"):
        lines.append(f"  note: {w['description']}")
    for ex in w.get("exercises") or []:
        vol = exercise_volume(ex)
        head = f"  - {ex.get('title')}"
        if vol:
            head += f"  ({vol:,.0f} kg)"
        lines.append(head)
        if ex.get("notes"):
            lines.append(f"      note: {ex['notes']}")
        for i, s in enumerate(ex.get("sets") or [], 1):
            lines.append(f"      {i}. {_fmt_set(s)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


USAGE = """Usage: uv run hevy.py <command> [options]

  me                              Account info
  list [--limit N] [--days N]     Recent workouts (summary lines)
  show <workout_id>               Full detail for one workout
  recent [--days N]               Full detail for every workout in a window
  history <query|template_id>     Logged history for one exercise
  routines                        Routines on the account
  sync                            Re-download the exercise template catalog
  search <query> [--limit N]      Search exercise templates by name
  resolve <query>                 Show how a name resolves to a template ID

Add --json to any command for raw JSON.
"""


def _arg(argv, flag, default=None, cast=str):
    if flag in argv:
        return cast(argv[argv.index(flag) + 1])
    return default


def _cli(argv):
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0

    cmd, rest = argv[0], argv[1:]
    as_json = "--json" in rest
    days = _arg(rest, "--days", None, int)
    limit = _arg(rest, "--limit", 10, int)
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    if cmd == "me":
        print(json.dumps(get_user_info(), indent=2))

    elif cmd in {"list", "recent"}:
        max_w = limit if days is None else 200
        workouts = list_workouts(since=since, max_workouts=max_w)
        if as_json:
            print(json.dumps(workouts, indent=2))
        else:
            for w in workouts:
                print(describe_workout(w) if cmd == "recent" else summarize_workout(w))
            if not workouts:
                print("(no workouts in range)")

    elif cmd == "show":
        w = get_workout(rest[0])
        payload = w.get("workout", w) if isinstance(w, dict) else w
        print(json.dumps(payload, indent=2) if as_json else describe_workout(payload))

    elif cmd == "history":
        target = rest[0]
        if not re.fullmatch(r"[0-9A-Fa-f-]{6,}", target):
            res = resolve_exercise(target)
            if res["status"] != "resolved":
                print(f"Could not resolve '{target}'. Candidates:", file=sys.stderr)
                for c in res["candidates"]:
                    print(f"  {c['score']:5.1f}  {c['template']['title']}", file=sys.stderr)
                return 1
            target = res["template"]["id"]
            print(f"# {res['template']['title']} ({target})")
        hist = exercise_history(target, start_date=since)
        print(json.dumps(hist, indent=2))

    elif cmd == "routines":
        routines = list_routines()
        if as_json:
            print(json.dumps(routines, indent=2))
        else:
            for r in routines:
                print(f"{r.get('id')}  {r.get('title')}  ({len(r.get('exercises') or [])} exercises)")

    elif cmd == "sync":
        templates = sync_catalog(force=True)
        custom = sum(1 for t in templates if t.get("is_custom"))
        print(f"Cached {len(templates)} exercise templates ({custom} custom) -> {CATALOG_FILE}")

    elif cmd == "search":
        for m in find_exercises(rest[0], limit=limit):
            t = m["template"]
            tag = " [custom]" if t.get("is_custom") else ""
            print(f"{m['score']:5.1f}  {t['id']}  {t['title']}  ({t.get('type')}){tag}")

    elif cmd == "resolve":
        res = resolve_exercise(rest[0])
        if as_json:
            print(json.dumps(res, indent=2))
            return 0
        print(f"status: {res['status']}")
        if res["template"]:
            t = res["template"]
            print(f"  -> {t['id']}  {t['title']}")
        for c in res["candidates"]:
            print(f"  {c['score']:5.1f}  {c['template']['title']}")

    else:
        print(f"Unknown command: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(_cli(sys.argv[1:]))
    except (HevyAPIError, HevyAuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
