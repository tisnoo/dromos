"""SQLite storage: connection setup, schema migrations, backups and activity upserts."""

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dromos.garmin import Run

DEFAULT_DB = "~/.dromos/dromos.db"

# Append-only: each entry runs once, in order, tracked via PRAGMA user_version.
MIGRATIONS = [
    """
    CREATE TABLE activities (
        id INTEGER PRIMARY KEY,
        garmin_id INTEGER NOT NULL UNIQUE,
        start_time TEXT NOT NULL,
        type TEXT NOT NULL,
        distance_km REAL NOT NULL,
        duration_s REAL NOT NULL,
        avg_pace_s_per_km REAL,
        avg_hr REAL,
        max_hr REAL,
        hr_zone_seconds TEXT NOT NULL,
        raw TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX activities_start_time ON activities (start_time);
    """,
    """
    CREATE TABLE plan_weeks (
        id INTEGER PRIMARY KEY,
        number INTEGER NOT NULL UNIQUE,
        week_start TEXT NOT NULL UNIQUE,
        phase TEXT NOT NULL CHECK (phase IN ('base', 'build', 'taper')),
        flag TEXT CHECK (flag IN ('cutback', 'peak', 'taper', 'race')),
        target_km REAL NOT NULL,
        long_run_km REAL NOT NULL,
        key_session TEXT,
        locked INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY,
        week_id INTEGER NOT NULL REFERENCES plan_weeks (id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        type TEXT NOT NULL CHECK (type IN (
            'easy', 'tempo', 'marathon_pace', 'interval', 'long', 'race', 'gym', 'mobility'
        )),
        title TEXT NOT NULL,
        target_km REAL,
        target_zone TEXT,
        notes TEXT
    );
    CREATE INDEX sessions_date ON sessions (date);
    CREATE TABLE plan_changes (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('claude', 'hermes', 'manual')),
        diff TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('proposed', 'applied', 'rejected'))
    );
    """,
    """
    CREATE TABLE oauth_clients (
        client_id TEXT PRIMARY KEY,
        info TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE oauth_tokens (
        token_hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
        grant_id TEXT NOT NULL,
        client_id TEXT NOT NULL,
        scopes TEXT NOT NULL,
        resource TEXT,
        expires_at INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX oauth_tokens_grant ON oauth_tokens (grant_id);
    """,
    # Lets a run count towards another day of the plan. Sync never touches it.
    "ALTER TABLE activities ADD COLUMN count_as_date TEXT;",
]


def default_path() -> Path:
    return Path(os.getenv("DROMOS_DB") or DEFAULT_DB).expanduser()


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the database (WAL mode) and apply any pending migrations."""
    db_path = path or str(default_path())
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN; {sql} PRAGMA user_version = {i}; COMMIT;")


def backup(conn: sqlite3.Connection, dest_dir: Path, keep: int = 14) -> Path:
    """Write a consistent copy of the database to dest_dir, keeping the newest `keep` copies."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"dromos-{datetime.now():%Y-%m-%d}.db"
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    for old in sorted(dest_dir.glob("dromos-*.db"))[:-keep]:
        old.unlink()
    return dest


def upsert_activity(conn: sqlite3.Connection, run: Run, raw: dict[str, Any]) -> bool:
    """Insert or update an activity by garmin_id. Returns True if it was new."""
    now = datetime.now().isoformat(timespec="seconds")
    exists = conn.execute(
        "SELECT 1 FROM activities WHERE garmin_id = ?", (run.garmin_id,)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO activities (
            garmin_id, start_time, type, distance_km, duration_s, avg_pace_s_per_km,
            avg_hr, max_hr, hr_zone_seconds, raw, ingested_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (garmin_id) DO UPDATE SET
            start_time = excluded.start_time,
            type = excluded.type,
            distance_km = excluded.distance_km,
            duration_s = excluded.duration_s,
            avg_pace_s_per_km = excluded.avg_pace_s_per_km,
            avg_hr = excluded.avg_hr,
            max_hr = excluded.max_hr,
            hr_zone_seconds = excluded.hr_zone_seconds,
            raw = excluded.raw,
            updated_at = excluded.updated_at
        """,
        (
            run.garmin_id,
            run.start_time.isoformat(),
            run.activity_type,
            run.distance_km,
            run.duration_s,
            run.avg_pace_s_per_km,
            run.avg_hr,
            run.max_hr,
            json.dumps(run.hr_zone_seconds),
            json.dumps(raw, default=str),
            now,
            now,
        ),
    )
    return exists is None


def set_count_as_date(conn: sqlite3.Connection, run_date: date, count_as: date | None) -> int:
    """Make the single run on run_date (Garmin's date) count as count_as; None clears it.

    Returns the run's garmin_id. Raises ValueError unless exactly one run is on run_date.
    """
    rows = conn.execute(
        "SELECT garmin_id FROM activities WHERE date(start_time) = ?", (run_date.isoformat(),)
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"expected one run on {run_date}, found {len(rows)}")
    garmin_id = rows[0]["garmin_id"]
    with conn:
        conn.execute(
            "UPDATE activities SET count_as_date = ? WHERE garmin_id = ?",
            (count_as.isoformat() if count_as else None, garmin_id),
        )
    return garmin_id


def latest_start_time(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(start_time) FROM activities").fetchone()
    return datetime.fromisoformat(row[0]) if row[0] else None
