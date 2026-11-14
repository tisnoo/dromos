"""Background jobs, run in-process by the app (BackgroundScheduler) or the CLI worker."""

import logging
import os
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.base import BaseScheduler

from dromos import db, notify
from dromos.ingest import sync_job

log = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS = 1
BACKUP_HOUR = 3


def backup_job() -> None:
    """Nightly copy of the database to DROMOS_BACKUP_DIR (default: backups/ next to the DB)."""
    dest_dir = Path(os.getenv("DROMOS_BACKUP_DIR") or db.default_path().parent / "backups")
    try:
        conn = db.connect()
        try:
            log.info("Database backed up to %s", db.backup(conn, dest_dir))
        finally:
            conn.close()
    except Exception:
        log.exception("Database backup failed")


def daily_brief_job() -> None:
    """Sync first, so last night's sleep and late runs are in, then send Hermes the brief."""
    if not notify.enabled():
        return
    sync_job()
    try:
        conn = db.connect()
        try:
            notify.send_daily_brief(conn)
        finally:
            conn.close()
    except Exception:
        log.exception("Daily brief failed")


def add_jobs(scheduler: BaseScheduler) -> BaseScheduler:
    scheduler.add_job(
        sync_job,
        "interval",
        hours=SYNC_INTERVAL_HOURS,
        next_run_time=datetime.now(),
        id="garmin_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(backup_job, "cron", hour=BACKUP_HOUR, id="db_backup", coalesce=True)
    scheduler.add_job(
        daily_brief_job,
        "cron",
        hour=notify.DAILY_BRIEF_HOUR,
        timezone=notify.timezone(),
        id="daily_brief",
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler
