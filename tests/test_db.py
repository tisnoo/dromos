import json
from datetime import datetime

from conftest import raw_run

from dromos import db
from dromos.garmin import normalise


def test_migrations_are_applied_once(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_upsert_inserts_then_updates(conn):
    raw = raw_run(1, "2026-11-10 07:15:00")
    assert db.upsert_activity(conn, normalise(raw), raw) is True

    edited = {**raw, "distance": 12000.0}
    assert db.upsert_activity(conn, normalise(edited), edited) is False

    rows = conn.execute("SELECT * FROM activities").fetchall()
    assert len(rows) == 1
    assert rows[0]["distance_km"] == 12.0
    assert json.loads(rows[0]["hr_zone_seconds"]) == {"2": 2700.0}
    assert json.loads(rows[0]["raw"])["distance"] == 12000.0


def test_latest_start_time(conn):
    assert db.latest_start_time(conn) is None
    for garmin_id, start in [(1, "2026-11-10 07:15:00"), (2, "2026-11-12 18:00:00")]:
        raw = raw_run(garmin_id, start)
        db.upsert_activity(conn, normalise(raw), raw)
    assert db.latest_start_time(conn) == datetime(2026, 11, 12, 18, 0)


def test_connect_creates_file_in_wal_mode(tmp_path):
    path = tmp_path / "sub" / "dromos.db"
    conn = db.connect(str(path))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    assert path.exists()


def test_backup_keeps_newest_copies(conn, tmp_path):
    for day in range(1, 4):
        (tmp_path / f"dromos-2026-01-0{day}.db").write_text("old")
    dest = db.backup(conn, tmp_path, keep=2)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["dromos-2026-01-03.db", dest.name]
    copy = db.connect(str(dest))
    assert copy.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    copy.close()
