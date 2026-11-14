from datetime import date

from conftest import raw_run, raw_sleep

from dromos import ingest


class FakeGarmin:
    def __init__(self, activities, nights=None):
        self.activities = activities
        self.nights = nights or {}
        self.calls = []
        self.sleep_calls = []

    def get_hrv_data(self, cdate):
        return None

    def get_sleep_data(self, cdate):
        self.sleep_calls.append(cdate)
        return self.nights.get(cdate, {"dailySleepDTO": {"calendarDate": cdate}})

    def get_activities_by_date(self, startdate, enddate, activitytype=None, sortorder=None):
        self.calls.append((startdate, enddate))
        return [a for a in self.activities if startdate <= a["startTimeLocal"][:10] <= enddate]


TODAY = date(2026, 11, 13)


def test_first_sync_backfills_and_rerun_is_safe(conn):
    client = FakeGarmin([raw_run(1, "2026-11-10 07:15:00"), raw_run(2, "2026-11-12 18:00:00")])
    new = ingest.sync(client, conn, today=TODAY)
    assert [r.garmin_id for r in new] == [1, 2]
    assert client.calls[0] == ("2025-11-13", "2026-11-13")

    assert ingest.sync(client, conn, today=TODAY) == []
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2


def test_later_sync_starts_shortly_before_latest_run(conn):
    client = FakeGarmin([raw_run(1, "2026-11-10 07:15:00")])
    ingest.sync(client, conn, today=TODAY)

    client.activities.append(raw_run(2, "2026-11-13 08:00:00"))
    new = ingest.sync(client, conn, today=TODAY)
    assert client.calls[-1] == ("2026-11-03", "2026-11-13")
    assert [r.garmin_id for r in new] == [2]


def test_explicit_since_overrides_default(conn):
    client = FakeGarmin([raw_run(1, "2026-07-09 18:00:00")])
    new = ingest.sync(client, conn, since=date(2026, 7, 1), today=TODAY)
    assert client.calls == [("2026-07-01", "2026-11-13")]
    assert len(new) == 1


def test_sync_job_logs_instead_of_raising(monkeypatch, caplog):
    def broken_connect(**kwargs):
        raise RuntimeError("token store missing")

    monkeypatch.setattr(ingest.garmin, "connect", broken_connect)
    ingest.sync_job()
    assert "Garmin sync failed" in caplog.text


def test_sleep_sync_backfills_then_refetches_recent_nights(conn):
    nights = {d: raw_sleep(d) for d in ("2026-11-11", "2026-11-12")}
    client = FakeGarmin([], nights)
    new = ingest.sync_sleep(client, conn, today=TODAY)
    assert len(client.sleep_calls) == ingest.SLEEP_INITIAL_LOOKBACK_DAYS + 1
    assert [n.calendar_date.isoformat() for n in new] == ["2026-11-11", "2026-11-12"]

    client.sleep_calls.clear()
    client.nights["2026-11-12"] = raw_sleep("2026-11-12", score=60)  # Garmin revised it
    client.nights["2026-11-13"] = raw_sleep("2026-11-13")
    new = ingest.sync_sleep(client, conn, today=TODAY)
    assert client.sleep_calls == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    assert [n.calendar_date.isoformat() for n in new] == ["2026-11-13"]
    scores = conn.execute("SELECT calendar_date, score FROM sleep ORDER BY 1").fetchall()
    assert [tuple(r) for r in scores] == [
        ("2026-11-11", 80),
        ("2026-11-12", 60),
        ("2026-11-13", 80),
    ]


def test_sync_job_still_syncs_sleep_when_runs_fail(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("DROMOS_DB", str(tmp_path / "dromos.db"))
    client = FakeGarmin([])
    client.get_activities_by_date = None  # breaks the run sync
    monkeypatch.setattr(ingest.garmin, "connect", lambda **kwargs: client)
    monkeypatch.setattr(ingest, "SLEEP_INITIAL_LOOKBACK_DAYS", 0)
    ingest.sync_job()
    assert "Garmin sync failed (runs)" in caplog.text
    assert len(client.sleep_calls) == 1  # just today


def test_sync_job_passes_new_runs_to_notify(monkeypatch, tmp_path):
    monkeypatch.setenv("DROMOS_DB", str(tmp_path / "dromos.db"))
    client = FakeGarmin([raw_run(1, f"{date.today()} 07:00:00")])
    monkeypatch.setattr(ingest.garmin, "connect", lambda **kwargs: client)
    monkeypatch.setattr(ingest, "SLEEP_INITIAL_LOOKBACK_DAYS", 0)
    notified = []
    monkeypatch.setattr(ingest.notify, "notify_new_runs", lambda conn, runs: notified.extend(runs))
    ingest.sync_job()
    ingest.sync_job()  # nothing new the second time
    assert [r.garmin_id for r in notified] == [1]
