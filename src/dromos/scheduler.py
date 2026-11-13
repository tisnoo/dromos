"""Background jobs, run in-process by the app (BackgroundScheduler) or the CLI worker."""

import logging
import os
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.base import BaseScheduler

from dromos import db
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
    return scheduler
