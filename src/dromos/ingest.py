"""Pull runs from Garmin into the database. Safe to re-run: activities upsert on garmin_id."""

import logging
import sqlite3
from datetime import date, timedelta

from garminconnect import Garmin

from dromos import db, garmin
from dromos.garmin import Run

log = logging.getLogger(__name__)

# Re-fetch a few days before the latest stored run, so edits made in Garmin
# afterwards (renames, corrected distances) and late-syncing runs get picked up.
OVERLAP_DAYS = 7
# How far back to look when the database is empty and no start date is given.
INITIAL_LOOKBACK_DAYS = 365


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


def sync_job() -> None:
    """Scheduled entry point: uses the token store only, never prompts."""
    try:
        client = garmin.connect(interactive=False)
        conn = db.connect()
        try:
            sync(client, conn)
        finally:
            conn.close()
    except Exception:
        log.exception("Garmin sync failed")
