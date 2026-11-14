"""Pull runs and sleep from Garmin into the database. Safe to re-run: both upsert."""

import logging
import sqlite3
from datetime import date, timedelta

from garminconnect import Garmin

from dromos import db, garmin
from dromos.garmin import Run, Sleep

log = logging.getLogger(__name__)

# Re-fetch a few days before the latest stored run, so edits made in Garmin
# afterwards (renames, corrected distances) and late-syncing runs get picked up.
OVERLAP_DAYS = 7
# How far back to look when the database is empty and no start date is given.
INITIAL_LOOKBACK_DAYS = 365
# Sleep is one Garmin call per night, so backfill less and re-fetch only the last few
# nights (scores can still change the day after, and today's may not be synced yet).
SLEEP_OVERLAP_DAYS = 2
SLEEP_INITIAL_LOOKBACK_DAYS = 60


def sync(
    client: Garmin,
    conn: sqlite3.Connection,
    since: date | None = None,
    today: date | None = None,
) -> list[Run]:
    """Fetch runs from `since` (or just before the latest stored run) to today.

    Returns the runs that were new to the database.
    """
    today = today or date.today()
    if since is None:
        latest = db.latest_start_time(conn)
        since = (
            latest.date() - timedelta(days=OVERLAP_DAYS)
            if latest
            else today - timedelta(days=INITIAL_LOOKBACK_DAYS)
        )

    new_runs = []
    with conn:
        for raw in garmin.fetch_raw_runs(client, since, today):
            run = garmin.normalise(raw)
            if db.upsert_activity(conn, run, raw):
                new_runs.append(run)
    log.info("Synced runs since %s: %d new", since, len(new_runs))
    return new_runs


def sync_sleep(
    client: Garmin,
    conn: sqlite3.Connection,
    since: date | None = None,
    today: date | None = None,
) -> list[Sleep]:
    """Fetch nights from `since` (or just before the latest stored night) to today.

    Returns the nights that were new to the database.
    """
    today = today or date.today()
    if since is None:
        latest = db.latest_sleep_date(conn)
        since = (
            latest - timedelta(days=SLEEP_OVERLAP_DAYS)
            if latest
            else today - timedelta(days=SLEEP_INITIAL_LOOKBACK_DAYS)
        )

    new_nights = []
    day = since
    while day <= today:
        raw = garmin.fetch_raw_sleep(client, day)
        night = garmin.normalise_sleep(raw)
        if night:
            with conn:
                if db.upsert_sleep(conn, night, garmin.sleep_summary(raw)):
                    new_nights.append(night)
        day += timedelta(days=1)
    log.info("Synced sleep since %s: %d new nights", since, len(new_nights))
    return new_nights


def sync_job() -> None:
    """Scheduled entry point: uses the token store only, never prompts."""
    try:
        client = garmin.connect(interactive=False)
        conn = db.connect()
    except Exception:
        log.exception("Garmin sync failed")
        return
    try:
        for name, fn in (("runs", sync), ("sleep", sync_sleep)):
            try:
                fn(client, conn)
            except Exception:
                log.exception("Garmin sync failed (%s)", name)
    finally:
        conn.close()
