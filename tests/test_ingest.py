from datetime import date

from conftest import raw_run

from dromos import ingest


class FakeGarmin:
    def __init__(self, activities):
        self.activities = activities
        self.calls = []

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
