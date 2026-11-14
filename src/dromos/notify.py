"""Push events to the Hermes agent, which turns them into coaching messages (e.g. on Telegram).

Two events, each POSTed as JSON to a Hermes webhook route:

- `dromos-run` (event_type `run_ingested`): a new run was synced, with the planned session
  for that day, the week so far and last night's sleep, so Hermes can give feedback on it.
- `dromos-daily` (event_type `daily_brief`): at DAILY_BRIEF_HOUR, today's session, the week
  ahead, the week so far, yesterday's runs and last night's sleep, so Hermes can say what
  to do today.

The payload carries everything needed, because Hermes webhook runs don't get MCP tools by
default; Hermes includes it in its prompt via `{__raw__}`, which cuts off at 4000 characters,
so payloads stay compact. Requests are signed with Hermes' generic V2 scheme: HMAC-SHA256
(hex) of `<timestamp>.<body>` in X-Webhook-Signature-V2, plus X-Webhook-Timestamp.

Off unless DROMOS_HERMES_WEBHOOK_URL (the base, e.g. http://127.0.0.1:8644/webhooks) and
DROMOS_HERMES_WEBHOOK_SECRET are set. A failed send is logged and dropped, never retried.
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from dromos import training
from dromos.garmin import Run

log = logging.getLogger(__name__)

RUN_ROUTE = "dromos-run"
DAILY_ROUTE = "dromos-daily"
DAILY_BRIEF_HOUR = 6
# Only runs from today and yesterday trigger feedback, so a backfill doesn't send hundreds.
RUN_MAX_AGE_DAYS = 1
TIMEOUT_S = 10
NEXT_DAYS = 7  # the daily brief looks a week ahead
SLEEP_FIELDS = (
    "score",
    "quality",
    "hours",
    "resting_hr",
    "avg_overnight_hrv",
    "hrv_status",
    "hrv_baseline",
    "body_battery_change",
)


def timezone() -> ZoneInfo:
    """DROMOS_TIMEZONE (e.g. Europe/Amsterdam): when 'today' starts and when the brief goes."""
    return ZoneInfo(os.getenv("DROMOS_TIMEZONE") or "UTC")


def local_today() -> date:
    return datetime.now(timezone()).date()


def enabled() -> bool:
    return all(os.getenv(k) for k in ("DROMOS_HERMES_WEBHOOK_URL", "DROMOS_HERMES_WEBHOOK_SECRET"))


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def send(route: str, payload: dict[str, Any], event_id: str) -> bool:
    """POST one event to Hermes. Returns False (and logs) when off or when it fails."""
    if not enabled():
        return False
    base = os.environ["DROMOS_HERMES_WEBHOOK_URL"].rstrip("/")
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": sign(os.environ["DROMOS_HERMES_WEBHOOK_SECRET"], timestamp, body),
        "X-Request-ID": event_id,  # Hermes drops repeats of the same id for an hour
    }
    try:
        response = httpx.post(f"{base}/{route}", content=body, headers=headers, timeout=TIMEOUT_S)
        response.raise_for_status()
    except httpx.HTTPError:
        log.exception("Sending %s to Hermes failed", event_id)
        return False
    log.info("Sent %s to Hermes", event_id)
    return True


def _compact(value: Any) -> Any:
    """Drop None values (recursively), to keep payloads under Hermes' 4000-char limit."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _sessions_on(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]] | str:
    sessions = [
        {k: s[k] for k in ("type", "title", "target_km", "target_zone", "notes")}
        for week in training.plan(conn, day, day)
        for s in week["sessions"]
        if s["date"] == day.isoformat()
    ]
    return sessions or "rest"


def _week_so_far(conn: sqlite3.Connection, day: date) -> dict[str, Any] | None:
    number = training.week_number_for(conn, day)
    if number is None:
        return None
    week = training.compare_week(conn, number, day)
    return {
        k: week[k]
        for k in (
            "week",
            "phase",
            "flag",
            "planned_km",
            "planned_km_so_far",
            "actual_km",
            "planned_long_run_km",
            "longest_actual_run_km",
        )
    }


def _night(conn: sqlite3.Connection, day: date) -> dict[str, Any] | None:
    night = next(iter(training.sleep(conn, day, day)), None)
    return {k: night[k] for k in SLEEP_FIELDS} if night else None


def _context(conn: sqlite3.Connection, day: date) -> dict[str, Any]:
    race = training.race_date(conn)
    return {
        "date": day.isoformat(),
        "days_to_race": (race - day).days if race else None,
        "this_week": _week_so_far(conn, day),
        "last_night": _night(conn, day),
        "heart_rate_targets": training.heart_rate_targets(conn),
    }


def run_payload(conn: sqlite3.Connection, run: Run) -> dict[str, Any] | None:
    """The run as AI clients see it, plus what was planned that day. None if it's gone."""
    row = conn.execute(
        "SELECT COALESCE(count_as_date, date(start_time)) FROM activities WHERE garmin_id = ?",
        (run.garmin_id,),
    ).fetchone()
    if row is None:
        return None
    day = date.fromisoformat(row[0])
    stored = next(
        (
            r
            for r in training.activities(conn, day, day)
            if r["start_time"] == run.start_time.isoformat()
        ),
        None,
    )
    return _compact(
        {
            "event_type": "run_ingested",
            "run": stored,
            "planned": _sessions_on(conn, day),
            **_context(conn, day),
        }
    )


def _next_days(conn: sqlite3.Connection, today: date) -> dict[str, str]:
    """The NEXT_DAYS days after today, one line each ("Sun 2026-11-15": "long: Long run 8 km")."""
    days = {}
    for offset in range(1, NEXT_DAYS + 1):
        day = today + timedelta(days=offset)
        sessions = _sessions_on(conn, day)
        days[f"{day:%a} {day}"] = (
            "; ".join(f"{s['type']}: {s['title']}" for s in sessions)
            if isinstance(sessions, list)
            else sessions
        )
    return days


def daily_payload(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    yesterday = today - timedelta(days=1)
    return _compact(
        {
            "event_type": "daily_brief",
            **_context(conn, today),
            "today": _sessions_on(conn, today),
            "next_days": _next_days(conn, today),
            "yesterday_planned": _sessions_on(conn, yesterday),
            "yesterday_runs": [
                {
                    k: r[k]
                    for k in (
                        "distance_km",
                        "duration_min",
                        "avg_pace",
                        "avg_hr",
                        "training_effect_label",
                    )
                }
                for r in training.activities(conn, yesterday, yesterday)
            ],
        }
    )


def notify_new_runs(conn: sqlite3.Connection, runs: list[Run], today: date | None = None) -> None:
    """Send feedback requests for recent new runs (older ones are a backfill: skipped)."""
    if not enabled():
        return
    oldest = (today or local_today()) - timedelta(days=RUN_MAX_AGE_DAYS)
    for run in runs:
        if run.start_time.date() < oldest:
            continue
        payload = run_payload(conn, run)
        if payload:
            send(RUN_ROUTE, payload, f"dromos-run-{run.garmin_id}")


def send_daily_brief(conn: sqlite3.Connection, today: date | None = None) -> bool:
    today = today or local_today()
    return send(DAILY_ROUTE, daily_payload(conn, today), f"dromos-daily-{today.isoformat()}")
