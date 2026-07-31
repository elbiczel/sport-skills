#!/usr/bin/env python3
"""
Strava activity fetcher.

Uses credentials/tokens stored by auth.py to call the Strava API and return
activities for the authenticated athlete.

Example:
    from fetch_activities import list_activities, summarize_activity

    for a in list_activities(per_page=10):
        print(summarize_activity(a))
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

from auth import get_access_token

API_BASE = "https://www.strava.com/api/v3"


def _api_get(path: str, params: Optional[dict] = None) -> dict | list:
    token = get_access_token()
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_athlete() -> dict:
    """Return the authenticated athlete's profile."""
    return _api_get("/athlete")  # type: ignore[return-value]


def list_activities(
    before: Optional[int | datetime] = None,
    after: Optional[int | datetime] = None,
    page: int = 1,
    per_page: int = 30,
) -> list[dict]:
    """
    Fetch a single page of the authenticated athlete's activities.

    `before`/`after` accept either an epoch integer or a datetime. Datetimes
    without tzinfo are interpreted as UTC.
    """
    params: dict = {"page": page, "per_page": per_page}
    if before is not None:
        params["before"] = _to_epoch(before)
    if after is not None:
        params["after"] = _to_epoch(after)
    return _api_get("/athlete/activities", params)  # type: ignore[return-value]


def iter_activities(
    before: Optional[int | datetime] = None,
    after: Optional[int | datetime] = None,
    per_page: int = 100,
    max_activities: Optional[int] = None,
) -> Iterator[dict]:
    """
    Yield activities across pages until Strava returns an empty page.

    Pass `max_activities` to cap the total yielded (and reduce API calls).
    """
    page = 1
    yielded = 0
    while True:
        batch = list_activities(
            before=before, after=after, page=page, per_page=per_page
        )
        if not batch:
            return
        for activity in batch:
            yield activity
            yielded += 1
            if max_activities is not None and yielded >= max_activities:
                return
        if len(batch) < per_page:
            return
        page += 1


def get_activity(activity_id: int, include_all_efforts: bool = False) -> dict:
    """Fetch a single activity by id (detailed representation)."""
    return _api_get(  # type: ignore[return-value]
        f"/activities/{activity_id}",
        {"include_all_efforts": str(include_all_efforts).lower()},
    )


def _to_epoch(value: int | datetime) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return int(value)


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_distance_km(meters: float) -> str:
    return f"{meters / 1000:.2f} km"


def _fmt_pace(meters: float, moving_seconds: int) -> str:
    """Return pace as min:sec/km; empty if distance is 0."""
    if meters <= 0 or moving_seconds <= 0:
        return ""
    secs_per_km = moving_seconds / (meters / 1000)
    m, s = divmod(int(secs_per_km), 60)
    return f"{m}:{s:02d}/km"


def _fmt_pace_from_speed(speed_mps: float) -> str:
    """Return pace as min:sec/km from a speed in m/s; empty if non-positive."""
    if not speed_mps or speed_mps <= 0:
        return ""
    secs_per_km = 1000.0 / speed_mps
    m, s = divmod(int(round(secs_per_km)), 60)
    return f"{m}:{s:02d}/km"


def format_splits(activity: dict) -> list[str]:
    """
    Return per-split (per-km) lines with pace and HR for a detailed activity.

    Reads the `splits_metric` array present on the detailed representation
    returned by `get_activity()`. Each line is:
        km N | <pace>/km | <HR> bpm | <±elev> m
    Returns an empty list if the activity carries no metric splits (e.g. it is
    a summary object from `list_activities`, or has no HR/distance data).
    """
    splits = activity.get("splits_metric") or []
    lines: list[str] = []
    for sp in splits:
        idx = sp.get("split", len(lines) + 1)
        pace = _fmt_pace_from_speed(sp.get("average_speed", 0) or 0)
        parts = [f"km {idx}"]
        if pace:
            parts.append(pace)
        hr = sp.get("average_heartrate")
        if hr:
            parts.append(f"{hr:.0f} bpm")
        elev = sp.get("elevation_difference")
        if elev is not None:
            parts.append(f"{elev:+.0f} m")
        # Partial final split (< ~1 km) is worth flagging so pace isn't misread.
        dist = sp.get("distance", 0) or 0
        if dist and dist < 900:
            parts.append(f"({dist / 1000:.2f} km)")
        lines.append(" | ".join(parts))
    return lines


def describe_activity_with_splits(activity: dict) -> str:
    """
    Return the one-line totals summary followed by an indented per-split
    breakdown (pace/HR per km). Falls back to just the summary line when the
    activity has no `splits_metric` (i.e. it wasn't fetched via get_activity).
    """
    out = [summarize_activity(activity)]
    for line in format_splits(activity):
        out.append(f"    {line}")
    return "\n".join(out)


def summarize_activity(activity: dict) -> str:
    """Return a short one-line summary of an activity dict."""
    name = activity.get("name", "(unnamed)")
    sport = activity.get("sport_type") or activity.get("type", "?")
    start = activity.get("start_date_local", "")[:10]
    distance = _fmt_distance_km(activity.get("distance", 0) or 0)
    moving = _fmt_duration(activity.get("moving_time", 0) or 0)
    pace = _fmt_pace(activity.get("distance", 0) or 0,
                     activity.get("moving_time", 0) or 0)
    parts = [start, sport, name, distance, moving]
    if pace:
        parts.append(pace)
    return " | ".join(str(p) for p in parts if p)


def _cmd_list(args: argparse.Namespace) -> None:
    activities = list_activities(
        page=args.page,
        per_page=args.per_page,
        before=args.before,
        after=args.after,
    )
    if args.json:
        print(json.dumps(activities, indent=2))
        return
    for a in activities:
        print(summarize_activity(a))


def _cmd_show(args: argparse.Namespace) -> None:
    activity = get_activity(args.id, include_all_efforts=args.efforts)
    print(json.dumps(activity, indent=2))


def _cmd_splits(args: argparse.Namespace) -> None:
    activity = get_activity(args.id)
    if args.json:
        print(json.dumps(activity.get("splits_metric", []), indent=2))
        return
    print(describe_activity_with_splits(activity))


def _cmd_me(_: argparse.Namespace) -> None:
    print(json.dumps(get_athlete(), indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Strava activities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List recent activities")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--per-page", type=int, default=30)
    p_list.add_argument("--before", type=int,
                        help="Epoch seconds upper bound")
    p_list.add_argument("--after", type=int,
                        help="Epoch seconds lower bound")
    p_list.add_argument("--json", action="store_true",
                        help="Print full JSON instead of summaries")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Show full JSON for one activity")
    p_show.add_argument("id", type=int)
    p_show.add_argument("--efforts", action="store_true",
                        help="Include all segment efforts")
    p_show.set_defaults(func=_cmd_show)

    p_splits = sub.add_parser(
        "splits", help="Show per-km split pace/HR for one activity")
    p_splits.add_argument("id", type=int)
    p_splits.add_argument("--json", action="store_true",
                          help="Print raw splits_metric JSON instead")
    p_splits.set_defaults(func=_cmd_splits)

    p_me = sub.add_parser("me", help="Show authenticated athlete profile")
    p_me.set_defaults(func=_cmd_me)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
