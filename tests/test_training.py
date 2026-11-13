from datetime import date

from conftest import raw_run
from test_plan import make_plan

from dromos import db, plan, training
from dromos.garmin import normalise


def load(conn):
    plan.import_plan(conn, make_plan(weeks=2), reason="test")
    for garmin_id, start, metres in [
        (1, "2026-11-09 18:00:00", 5200.0),
        (2, "2026-11-11 07:00:00", 4800.0),
        (3, "2026-11-16 07:00:00", 6000.0),
    ]:
        raw = raw_run(garmin_id, start, distance_m=metres)
        db.upsert_activity(conn, normalise(raw), raw)


def test_activities_formats_runs(conn):
    load(conn)
    runs = training.activities(conn, date(2026, 11, 9), date(2026, 11, 15))
    assert [r["distance_km"] for r in runs] == [5.2, 4.8]
    assert runs[0]["avg_pace"] == "10:35/km"
    assert runs[0]["hr_zone_minutes"] == {"Z2": 45.0}


def test_plan_returns_overlapping_weeks(conn):
    load(conn)
    weeks = training.plan(conn, date(2026, 11, 14), date(2026, 11, 16))
    assert [w["week"] for w in weeks] == [1, 2]
    assert weeks[0]["end"] == "2026-11-15"
    assert {s["type"] for s in weeks[0]["sessions"]} == {"easy", "gym"}


def test_week_lookup_and_race_date(conn):
    load(conn)
    assert training.week_number_for(conn, date(2026, 11, 15)) == 1
    assert training.week_number_for(conn, date(2026, 11, 16)) == 2
    assert training.week_number_for(conn, date(2026, 12, 1)) is None
    assert training.race_date(conn) is None


def test_compare_week(conn):
    load(conn)
    result = training.compare_week(conn, 1, today=date(2026, 11, 10))
    assert result["planned_km"] == 5
    assert result["planned_km_so_far"] == 5
    assert result["actual_km"] == 10.0
    assert result["longest_actual_run_km"] == 5.2
    assert result["days_elapsed"] == 2
    assert result["hr_zone_minutes"] == {"Z2": 90.0}
    monday, tuesday = result["days"][0], result["days"][1]
    assert [p["type"] for p in monday["planned"]] == ["easy", "gym"]
    assert monday["actual_runs"][0]["distance_km"] == 5.2
    assert tuesday["planned"] == "rest"


def test_moved_run_counts_on_its_new_day(conn):
    load(conn)
    db.set_count_as_date(conn, date(2026, 11, 16), date(2026, 11, 15))
    runs = training.activities(conn, date(2026, 11, 9), date(2026, 11, 15))
    assert [(r["date"], r["moved"]) for r in runs][-1] == ("2026-11-15", True)
    assert runs[-1]["start_time"].startswith("2026-11-16")
    assert training.activities(conn, date(2026, 11, 16), date(2026, 11, 16)) == []
