from apscheduler.schedulers.background import BackgroundScheduler

from dromos.ingest import sync_job
from dromos.scheduler import add_jobs


def test_sync_runs_hourly():
    scheduler = add_jobs(BackgroundScheduler())
    job = scheduler.get_job("garmin_sync")
    assert job.func is sync_job
    assert job.trigger.interval.total_seconds() == 3600


def test_backup_runs_nightly():
    job = add_jobs(BackgroundScheduler()).get_job("db_backup")
    assert str(job.trigger) == "cron[hour='3']"
