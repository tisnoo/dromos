"""Training plan import.

A plan file is JSON:

    {"weeks": [{
        "number": 1, "week_start": "2026-11-09", "phase": "base", "flag": null,
        "target_km": 22, "long_run_km": 8, "key_session": "Easy run 4 km", "locked": false,
        "sessions": [{"date": "2026-11-09", "type": "easy", "title": "Easy run 5 km",
                      "target_km": 5, "target_zone": "Z1", "notes": "..."}]
    }]}

Weeks start on Monday; days without a session are rest days. Plan files hold personal
training data, so keep them out of git (e.g. in data/).
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

PHASES = {"base", "build", "taper"}
FLAGS = {None, "cutback", "peak", "taper", "race"}
SESSION_TYPES = {"easy", "tempo", "marathon_pace", "interval", "long", "race", "gym", "mobility"}


class PlanError(ValueError):
    pass


def validate(plan: dict[str, Any]) -> None:
    """Check a plan file's structure before anything is written."""
    weeks = plan.get("weeks") or []
    if not weeks:
        raise PlanError("plan has no weeks")
    previous_start = None
    for i, week in enumerate(weeks, start=1):
        where = f"week {week.get('number', i)}"
        if week.get("number") != i:
            raise PlanError(f"{where}: weeks must be numbered 1, 2, 3, ... in order")
        start = date.fromisoformat(week["week_start"])
        if start.weekday() != 0:
            raise PlanError(f"{where}: week_start {start} is not a Monday")
        if previous_start and start != previous_start + timedelta(days=7):
            raise PlanError(f"{where}: weeks must be consecutive")
        previous_start = start
        if week.get("phase") not in PHASES:
            raise PlanError(f"{where}: phase must be one of {sorted(PHASES)}")
        if week.get("flag") not in FLAGS:
            raise PlanError(f"{where}: unknown flag {week.get('flag')!r}")
        for session in week.get("sessions", []):
            day = date.fromisoformat(session["date"])
            if not start <= day < start + timedelta(days=7):
                raise PlanError(f"{where}: session on {day} is outside the week")
            if session.get("type") not in SESSION_TYPES:
                raise PlanError(f"{where}: unknown session type {session.get('type')!r}")
            if not session.get("title"):
                raise PlanError(f"{where}: session on {day} has no title")


def has_plan(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM plan_weeks LIMIT 1").fetchone() is not None


def import_plan(
    conn: sqlite3.Connection, plan: dict[str, Any], reason: str, replace: bool = False
) -> None:
    """Load a plan into an empty database (or replace the current one) and log it."""
    validate(plan)
    with conn:
        if has_plan(conn):
            if not replace:
                raise PlanError("a plan is already loaded; pass replace=True to overwrite it")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM plan_weeks")
        for week in plan["weeks"]:
            week_id = conn.execute(
                """
                INSERT INTO plan_weeks (number, week_start, phase, flag, target_km,
                                        long_run_km, key_session, locked,
                                        original_target_km, original_long_run_km)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    week["number"],
                    week["week_start"],
                    week["phase"],
                    week.get("flag"),
                    week["target_km"],
                    week["long_run_km"],
                    week.get("key_session"),
                    int(bool(week.get("locked"))),
                    week["target_km"],
                    week["long_run_km"],
                ),
            ).lastrowid
            conn.executemany(
                """
                INSERT INTO sessions (week_id, date, type, title, target_km, target_zone, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        week_id,
                        s["date"],
                        s["type"],
                        s["title"],
                        s.get("target_km"),
                        s.get("target_zone"),
                        s.get("notes"),
                    )
                    for s in week.get("sessions", [])
                ],
            )
        summary = {
            "action": "replace" if replace else "import",
            "weeks": len(plan["weeks"]),
            "sessions": sum(len(w.get("sessions", [])) for w in plan["weeks"]),
            "first_week": plan["weeks"][0]["week_start"],
            "last_week": plan["weeks"][-1]["week_start"],
        }
        conn.execute(
            """
            INSERT INTO plan_changes (created_at, reason, source, diff, status)
            VALUES (?, ?, 'manual', ?, 'applied')
            """,
            (datetime.now().isoformat(timespec="seconds"), reason, json.dumps(summary)),
        )
