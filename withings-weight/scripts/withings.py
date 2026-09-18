#!/usr/bin/env python3
"""
Withings weigh-in reader.

Pulls body measurements (weight + whatever body composition the scale recorded)
from `POST https://wbsapi.withings.net/measure` (`action=getmeas`) and formats
them for a weekly training review.

Weight is the number to trend. The scale's impedance body-fat figure is known
to disagree with Tomek's DEXA scan, so the composition fields are printed for
context only - don't build logic on them.

Layering, deliberately: everything between `MEASTYPES` and `format_weekly_rows`
is pure - it takes decoded API payloads and returns data, touching no network.
That is what `selftest.py` exercises without credentials.

Library entry points:
    list_measurements(since=None, until=None)
    latest_measurement()
    weekly_summary(weeks=8)
    describe_measurement(reading)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402  (same directory, and auth imports this lazily)

MEASURE_URL = "https://wbsapi.withings.net/measure"

# Withings meastype codes. Units are what the API documents, NOT what you might
# assume: hydration (77) is kilograms of water, not a percentage.
MEASTYPES = {
    1: ("weight", "kg"),
    5: ("fat_free_mass", "kg"),
    6: ("fat_ratio", "%"),
    8: ("fat_mass", "kg"),
    76: ("muscle_mass", "kg"),
    77: ("hydration", "kg"),
    88: ("bone_mass", "kg"),
}
FIELD_ORDER = [MEASTYPES[t][0] for t in (1, 6, 8, 5, 76, 88, 77)]

CATEGORY_REAL = 1       # 1 = real measures, 2 = user objectives
DEFAULT_TZ = "Europe/Zurich"
MAX_PAGES = 50          # guard against a pagination loop that never sets more=0

# How the group was attributed to the user (`attrib`).
ATTRIB_LABELS = {
    0: "device", 1: "device (ambiguous)", 2: "manual", 4: "manual (signup)",
    5: "device (auto)", 7: "confirmed", 8: "device", 15: "guided",
}


# ---------------------------------------------------------------------------
# pure: decoding
# ---------------------------------------------------------------------------

# A weigh-in counts as a comparable morning reading when it is the first of its
# local day and was taken before this hour. Evening readings run ~0.5-1 kg
# heavier and would drag a weekly mean upward.
MORNING_CUTOFF_HOUR = 11


def resolve_timezone(body_timezone: str | None = None, name: str = DEFAULT_TZ):
    """Return a tzinfo, preferring the user's zone, then the API's, then UTC.

    `zoneinfo` needs a tz database, which is missing on some minimal systems -
    hence the fallbacks rather than an exception.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return timezone.utc
    for candidate in (name, body_timezone):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except Exception:  # noqa: BLE001 - any tzdata failure means "try the next"
            continue
    return timezone.utc


def measure_value(measure: dict) -> float:
    """Decode one measure: real value = value * 10^unit."""
    return float(measure["value"]) * (10 ** int(measure["unit"]))


def merge_measure_pages(pages) -> list:
    """Flatten paginated `measuregrps` lists, dropping groups repeated across pages.

    Withings pages with `more`/`offset`, and an overlapping offset can hand back
    a group twice; `grpid` is the stable identity.
    """
    merged, seen = [], set()
    for page in pages:
        for grp in page or []:
            gid = grp.get("grpid")
            if gid is not None and gid in seen:
                continue
            if gid is not None:
                seen.add(gid)
            merged.append(grp)
    return merged


def parse_group(grp: dict, tz) -> dict:
    """Turn one measure group into a flat reading dict with local date/time."""
    dt = datetime.fromtimestamp(int(grp["date"]), tz)
    reading = {
        "grpid": grp.get("grpid"),
        "timestamp": int(grp["date"]),
        "datetime": dt,
        "date": dt.strftime("%Y-%m-%d"),
        "weekday": dt.strftime("%a"),
        "time": dt.strftime("%H:%M"),
        "attrib": grp.get("attrib"),
        "source": ATTRIB_LABELS.get(grp.get("attrib"), "unknown"),
        "model": grp.get("model"),
        "first_of_day": False,
        "day_count": 1,
    }
    for name, _unit in MEASTYPES.values():
        reading[name] = None
    for measure in grp.get("measures") or []:
        mapping = MEASTYPES.get(measure.get("type"))
        if mapping:
            reading[mapping[0]] = measure_value(measure)
    return reading


def annotate_days(readings: list) -> list:
    """Flag the earliest reading of each local day (the comparable fasted one)."""
    by_day = {}
    for r in readings:
        by_day.setdefault(r["date"], []).append(r)
    for day_readings in by_day.values():
        earliest = min(day_readings, key=lambda r: r["timestamp"])
        for r in day_readings:
            r["day_count"] = len(day_readings)
            r["first_of_day"] = r is earliest
    return readings


def parse_measure_groups(groups: list, tz) -> list:
    """Decode groups into readings, newest first, with first-of-day flags."""
    readings = [parse_group(g, tz) for g in groups if g.get("date") is not None]
    readings.sort(key=lambda r: r["timestamp"], reverse=True)
    return annotate_days(readings)


# ---------------------------------------------------------------------------
# pure: weekly selection
# ---------------------------------------------------------------------------

def weekly_rows(readings: list, weeks: int | None = 8) -> list:
    """Reduce readings to one comparable row per ISO week, oldest first.

    Friday morning is the anchor: the plan's weigh-in day. When a week has no
    Friday reading the row falls back to the first reading of that week's latest
    day, and `note` says which day was actually used so a substitution is never
    silently read as a Friday.

    Each row also carries the week's **morning readings** (first of the day,
    before MORNING_CUTOFF_HOUR), their mean and the change in that mean versus
    the previous week. With three or more mornings in a week the morning mean is
    the better trend figure: a single reading carries ~0.5 kg of day-to-day
    noise from water, salt and glycogen, and averaging removes most of it.
    """
    buckets = {}
    for r in readings:
        iso = r["datetime"].isocalendar()
        buckets.setdefault((iso[0], iso[1]), []).append(r)

    rows = []
    for (iso_year, iso_week) in sorted(buckets):
        items = sorted(buckets[(iso_year, iso_week)], key=lambda r: r["timestamp"])
        fridays = [r for r in items if r["datetime"].isocalendar()[2] == 5]
        if fridays:
            chosen = fridays[0]
            note = "Friday" + (f", 1st of {len([r for r in fridays])}" if len(fridays) > 1 else "")
        else:
            latest_day = items[-1]["date"]
            chosen = next(r for r in items if r["date"] == latest_day)
            note = f"no Friday reading, used {chosen['weekday']}"

        weights = [r["weight"] for r in items if r["weight"] is not None]
        mornings = [r for r in items
                    if r.get("first_of_day") and r["weight"] is not None
                    and r["datetime"].hour < MORNING_CUTOFF_HOUR]
        morning_weights = [r["weight"] for r in mornings]
        morning_fat = [r["fat_ratio"] for r in mornings if r.get("fat_ratio") is not None]
        rows.append({
            "iso_year": iso_year,
            "iso_week": iso_week,
            "label": f"{iso_year}-W{iso_week:02d}",
            "date": chosen["date"],
            "weekday": chosen["weekday"],
            "time": chosen["time"],
            "weight": round(chosen["weight"], 2) if chosen["weight"] is not None else None,
            "note": note,
            "n_readings": len(items),
            "mean_weight": round(sum(weights) / len(weights), 2) if weights else None,
            "delta": None,
            "n_mornings": len(mornings),
            "morning_mean": (round(sum(morning_weights) / len(morning_weights), 2)
                             if morning_weights else None),
            "morning_delta": None,
            # Impedance body fat: biased low against the DEXA (see SKILL.md), so
            # only its slow trend means anything. Same morning readings, averaged.
            "morning_fat_mean": (round(sum(morning_fat) / len(morning_fat), 1)
                                 if morning_fat else None),
            "mornings": [{"date": r["date"], "weekday": r["weekday"], "time": r["time"],
                          "weight": round(r["weight"], 2),
                          "fat_ratio": (round(r["fat_ratio"], 1)
                                        if r.get("fat_ratio") is not None else None)}
                         for r in mornings],
        })

    # Deltas come off the rounded weights so the column always reconciles with
    # the two weights either side of it.
    previous = None
    for row in rows:
        if previous is not None and row["weight"] is not None:
            row["delta"] = round(row["weight"] - previous, 2)
        if row["weight"] is not None:
            previous = row["weight"]

    # Same again for the morning mean, which is the trend to judge the diet on
    # once a week holds several morning readings.
    previous = None
    for row in rows:
        if previous is not None and row["morning_mean"] is not None:
            row["morning_delta"] = round(row["morning_mean"] - previous, 2)
        if row["morning_mean"] is not None:
            previous = row["morning_mean"]

    return rows[-weeks:] if weeks else rows


# ---------------------------------------------------------------------------
# pure: formatting
# ---------------------------------------------------------------------------

def _composition(reading: dict) -> str:
    parts = []
    for field in FIELD_ORDER:
        if field == "weight":
            continue
        value = reading.get(field)
        if value is None:
            continue
        label = {
            "fat_ratio": "fat", "fat_mass": "fat mass", "fat_free_mass": "lean",
            "muscle_mass": "muscle", "bone_mass": "bone", "hydration": "water",
        }[field]
        unit = "%" if field == "fat_ratio" else " kg"
        parts.append(f"{label} {value:.2f}{unit}" if unit == " kg"
                     else f"{label} {value:.1f}{unit}")
    return "  ".join(parts)


def describe_measurement(reading: dict) -> str:
    """One-line summary of a weigh-in: date, weekday, local time, weight, composition."""
    weight = f"{reading['weight']:.2f} kg" if reading.get("weight") is not None else "  -  "
    line = f"{reading['date']}  {reading['weekday']}  {reading['time']}  {weight:>9}"
    comp = _composition(reading)
    if comp:
        line += f"   {comp}"
    if reading.get("first_of_day") and reading.get("day_count", 1) > 1:
        line += f"   * first of {reading['day_count']} today"
    return line


def format_weekly_rows(rows: list) -> list:
    """Render weekly rows as fixed-width lines (header first), oldest week first."""
    out = [
        f"{'week':<9} {'used':<16} {'weight':>8} {'delta':>7} {'mean':>8}  note",
        f"{'-'*9} {'-'*16} {'-'*8} {'-'*7} {'-'*8}  {'-'*4}",
    ]
    for row in rows:
        used = f"{row['date']} {row['weekday']}"
        weight = f"{row['weight']:.2f}" if row["weight"] is not None else "-"
        delta = f"{row['delta']:+.2f}" if row["delta"] is not None else "-"
        mean = f"{row['mean_weight']:.2f}" if row["mean_weight"] is not None else "-"
        out.append(f"{row['label']:<9} {used:<16} {weight:>8} {delta:>7} {mean:>8}  "
                   f"{row['note']} ({row['n_readings']} reading"
                   f"{'s' if row['n_readings'] != 1 else ''})")
    return out


def format_weekly_trend(rows: list) -> list:
    """Weekly table led by the morning mean, with every morning reading listed.

    This is what the `weekly` command prints. `format_weekly_rows` keeps the
    older single-reading layout.
    """
    out = [
        f"{'week':<9} {'mornings':>8} {'mean':>7} {'delta':>7} {'Friday':>7} {'fat%':>6}  morning readings",
        f"{'-'*9} {'-'*8} {'-'*7} {'-'*7} {'-'*7} {'-'*6}  {'-'*16}",
    ]
    for row in rows:
        mean = f"{row['morning_mean']:.2f}" if row["morning_mean"] is not None else "-"
        delta = f"{row['morning_delta']:+.2f}" if row["morning_delta"] is not None else "-"
        friday = (f"{row['weight']:.2f}" if row["note"].startswith("Friday")
                  and row["weight"] is not None else "-")
        listing = " · ".join(f"{m['weekday']} {m['weight']:.2f}" for m in row["mornings"]) or "none"
        other = row["n_readings"] - row["n_mornings"]
        if other:
            listing += f"   (+{other} other, not averaged)"
        fat = f"{row['morning_fat_mean']:.1f}" if row.get("morning_fat_mean") is not None else "-"
        out.append(f"{row['label']:<9} {row['n_mornings']:>8} {mean:>7} {delta:>7} {friday:>7} {fat:>6}  {listing}")
    out.append("")
    out.append("Trend = change in the morning mean, week on week. Trust it when both weeks "
               "have 3+ mornings; with fewer, compare Friday to Friday.")
    out.append("fat% = mean impedance body fat on those mornings. It reads ~10 points under the "
               "Sept 2026 DEXA (21.6 %), so read its direction over 4+ weeks, never its level.")
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _api_post(url: str, params: dict) -> dict:
    """POST to the Withings API, refreshing the token once on an auth failure."""
    token = auth.get_access_token()
    payload = auth._post_form(url, params, {"Authorization": f"Bearer {token}"})
    try:
        return auth.unwrap(payload)
    except auth.WithingsAPIError as e:
        if e.status not in auth.INVALID_TOKEN_STATUSES:
            raise
        # Token rejected despite a live expiry - force a refresh and retry once.
        token = auth.get_access_token(force_refresh=True)
        payload = auth._post_form(url, params, {"Authorization": f"Bearer {token}"})
        return auth.unwrap(payload)


def get_measure_groups(startdate=None, enddate=None, meastypes=None,
                       category: int = CATEGORY_REAL):
    """Fetch raw measure groups, following `more`/`offset` pagination to the end.

    Returns (groups, body_timezone).
    """
    types = meastypes or sorted(MEASTYPES)
    params = {
        "action": "getmeas",
        "meastypes": ",".join(str(t) for t in types),
        "category": category,
    }
    if startdate is not None:
        params["startdate"] = int(startdate)
    if enddate is not None:
        params["enddate"] = int(enddate)

    pages, body_tz, offset = [], None, None
    for _ in range(MAX_PAGES):
        call = dict(params)
        if offset:
            call["offset"] = offset
        body = _api_post(MEASURE_URL, call)
        body_tz = body.get("timezone") or body_tz
        pages.append(body.get("measuregrps") or [])
        if not body.get("more"):
            break
        offset = body.get("offset")
        if not offset:
            break
    return merge_measure_pages(pages), body_tz


def list_measurements(since=None, until=None) -> list:
    """Return weigh-ins between `since` and `until` (datetimes or unix seconds), newest first."""
    def _stamp(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=resolve_timezone())
            return int(value.timestamp())
        return int(value)

    groups, body_tz = get_measure_groups(_stamp(since), _stamp(until))
    return parse_measure_groups(groups, resolve_timezone(body_tz))


def latest_measurement(lookback_days: int = 90):
    """Return the most recent weigh-in, or None if there is none in `lookback_days`."""
    since = datetime.now(resolve_timezone()) - timedelta(days=lookback_days)
    readings = list_measurements(since=since)
    return readings[0] if readings else None


def weekly_summary(weeks: int = 8) -> list:
    """One comparable row per ISO week for the last `weeks` weeks, oldest first.

    See `weekly_rows` for the Friday-first selection rule. This is the table the
    training plan copies.
    """
    tz = resolve_timezone()
    now = datetime.now(tz)
    week_start = (now - timedelta(days=now.isoweekday() - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    since = week_start - timedelta(weeks=max(weeks - 1, 0))
    return weekly_rows(list_measurements(since=since), weeks=weeks)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _raw_json(readings: list) -> str:
    return json.dumps([
        {k: v for k, v in r.items() if k != "datetime"} for r in readings
    ], indent=2)


def _cmd_list(args) -> None:
    since = datetime.now(resolve_timezone()) - timedelta(days=args.days)
    readings = list_measurements(since=since)
    if args.json:
        print(_raw_json(readings))
        return
    if not readings:
        print(f"No weigh-ins in the last {args.days} days.")
        return
    for reading in readings:
        print(describe_measurement(reading))


def _cmd_latest(args) -> None:
    reading = latest_measurement()
    if args.json:
        print(_raw_json([reading] if reading else []))
        return
    if not reading:
        print("No weigh-ins found in the last 90 days.")
        return
    print(describe_measurement(reading))


def _cmd_weekly(args) -> None:
    rows = weekly_summary(weeks=args.weeks)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print(f"No weigh-ins in the last {args.weeks} weeks.")
        return
    for line in format_weekly_trend(rows):
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read Withings weigh-ins")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="One line per weigh-in, newest first")
    p.add_argument("--days", type=int, default=28)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("latest", help="The most recent weigh-in")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_latest)

    p = sub.add_parser("weekly", help="One comparable row per ISO week")
    p.add_argument("--weeks", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_weekly)

    args = parser.parse_args()
    try:
        args.func(args)
    except (auth.WithingsAuthError, auth.WithingsAPIError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
