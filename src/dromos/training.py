"""Read-side queries over runs and the plan, shaped for AI clients (plain dicts, km, m:ss)."""

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

RUN_SESSION_TYPES = ("easy", "tempo", "marathon_pace", "interval", "long", "race")


def fmt_pace(seconds_per_km: float | None) -> str | None:
    if not seconds_per_km:
        return None
    minutes, seconds = divmod(round(seconds_per_km), 60)
    return f"{minutes}:{seconds:02d}/km"


def activities(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM activities
        WHERE date(start_time) BETWEEN ? AND ?
        ORDER BY start_time
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        {
            "date": row["start_time"][:10],
            "start_time": row["start_time"],
            "type": row["type"],
            "distance_km": round(row["distance_km"], 2),
            "duration_min": round(row["duration_s"] / 60, 1),
            "avg_pace": fmt_pace(row["avg_pace_s_per_km"]),
            "avg_hr": row["avg_hr"],
            "max_hr": row["max_hr"],
            "hr_zone_minutes": {
                f"Z{zone}": round(seconds / 60, 1)
                for zone, seconds in json.loads(row["hr_zone_seconds"]).items()
            },
        }
        for row in rows
    ]


def _sessions(conn: sqlite3.Connection, week_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sessions WHERE week_id = ? ORDER BY date, id", (week_id,)
    ).fetchall()
    return [
        {
            "date": row["date"],
            "type": row["type"],
            "title": row["title"],
            "target_km": row["target_km"],
            "target_zone": row["target_zone"],
            "notes": row["notes"],
        }
        for row in rows
    ]


def _week(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    start = date.fromisoformat(row["week_start"])
    return {
        "week": row["number"],
        "start": row["week_start"],
        "end": (start + timedelta(days=6)).isoformat(),
        "phase": row["phase"],
        "flag": row["flag"],
        "target_km": row["target_km"],
        "long_run_km": row["long_run_km"],
        "key_session": row["key_session"],
        "locked": bool(row["locked"]),
        "sessions": _sessions(conn, row["id"]),
    }


def plan(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """Plan weeks overlapping [start, end], with their sessions. Missing days are rest days."""
    rows = conn.execute(
        "SELECT * FROM plan_weeks WHERE week_start <= ? AND date(week_start, '+6 days') >= ? "
        "ORDER BY number",
        (end.isoformat(), start.isoformat()),
    ).fetchall()
    return [_week(conn, row) for row in rows]


def week_number_for(conn: sqlite3.Connection, day: date) -> int | None:
    row = conn.execute(
        "SELECT number FROM plan_weeks WHERE week_start <= ? AND date(week_start, '+6 days') >= ?",
        (day.isoformat(), day.isoformat()),
    ).fetchone()
    return row["number"] if row else None


def race_date(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MIN(date) FROM sessions WHERE type = 'race'").fetchone()
    return date.fromisoformat(row[0]) if row[0] else None


def compare_week(conn: sqlite3.Connection, number: int, today: date) -> dict[str, Any]:
    """Planned vs actual for one plan week, day by day."""
    row = conn.execute("SELECT * FROM plan_weeks WHERE number = ?", (number,)).fetchone()
    if row is None:
        raise ValueError(f"There is no week {number} in the plan")
    week = _week(conn, row)
    start, end = date.fromisoformat(week["start"]), date.fromisoformat(week["end"])
    runs = activities(conn, start, end)

    days = []
    for offset in range(7):
        day = (start + timedelta(days=offset)).isoformat()
        planned = [s for s in week["sessions"] if s["date"] == day]
        actual = [r for r in runs if r["date"] == day]
        days.append(
            {
                "date": day,
                "planned": [
                    {k: s[k] for k in ("type", "title", "target_km", "target_zone")}
                    for s in planned
                ]
                or "rest",
                "actual_runs": [
                    {k: r[k] for k in ("distance_km", "duration_min", "avg_pace", "avg_hr")}
                    for r in actual
                ],
            }
        )

    planned_km = sum(
        s["target_km"] or 0 for s in week["sessions"] if s["type"] in RUN_SESSION_TYPES
    )
    actual_km = round(sum(r["distance_km"] for r in runs), 2)
    elapsed = min(max((today - start).days + 1, 0), 7)
    planned_so_far = sum(
        s["target_km"] or 0
        for s in week["sessions"]
        if s["type"] in RUN_SESSION_TYPES and s["date"] <= today.isoformat()
    )
    zone_minutes: dict[str, float] = {}
    for run in runs:
        for zone, minutes in run["hr_zone_minutes"].items():
            zone_minutes[zone] = round(zone_minutes.get(zone, 0) + minutes, 1)

    return {
        "week": week["week"],
        "start": week["start"],
        "end": week["end"],
        "phase": week["phase"],
        "flag": week["flag"],
        "days_elapsed": elapsed,
        "planned_km": planned_km,
        "planned_km_so_far": planned_so_far,
        "actual_km": actual_km,
        "planned_long_run_km": week["long_run_km"],
        "longest_actual_run_km": max((r["distance_km"] for r in runs), default=0),
        "hr_zone_minutes": dict(sorted(zone_minutes.items())),
        "days": days,
    }
