from datetime import date

from conftest import raw_run, raw_sleep
from test_plan import make_plan

from dromos import db, plan, training
from dromos.garmin import normalise, normalise_sleep


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


def test_sleep_formats_nights(conn):
    for day in ("2026-11-12", "2026-11-13"):
        raw = raw_sleep(day)
        db.upsert_sleep(conn, normalise_sleep(raw), raw)
    nights = training.sleep(conn, date(2026, 11, 13), date(2026, 11, 13))
    assert nights == [
        {
            "date": "2026-11-13",
            "score": 80,
            "quality": "GOOD",
            "hours": 7.5,
            "deep_min": 90,
            "light_min": 270,
            "rem_min": 90,
            "awake_min": 10,
            "awake_count": 1,
            "avg_sleep_stress": 15.0,
            "resting_hr": 55.0,
            "avg_overnight_hrv": 60.0,
            "hrv_status": "BALANCED",
            "body_battery_change": 50.0,
            "feedback": "POSITIVE_LONG_AND_DEEP",
            "hrv_weekly_avg": 58.0,
            "hrv_baseline": [50.0, 80.0],
        }
    ]


def test_activities_include_garmin_run_details(conn):
    raw = raw_run(
        1,
        "2026-11-13 10:00:00",
        aerobicTrainingEffect=5.0,
        trainingEffectLabel="LACTATE_THRESHOLD",
        activityTrainingLoad=138.60275,
        averageRunningCadenceInStepsPerMinute=139.09375,
    )
    db.upsert_activity(conn, normalise(raw), raw)
    run = training.activities(conn, date(2026, 11, 13), date(2026, 11, 13))[0]
    assert run["training_effect_label"] == "LACTATE_THRESHOLD"
    assert (run["aerobic_training_effect"], run["training_load"]) == (5.0, 138.6)
    assert run["cadence_spm"] == 139.1
    assert run["vo2max"] is None


def test_heart_rate_targets_follow_the_latest_test(conn):
    assert training.heart_rate_targets(conn) is None
    db.add_threshold(conn, date(2026, 11, 1), 170)
    db.add_threshold(conn, date(2026, 12, 1), 180, training.parse_pace("5:10"))
    assert training.heart_rate_targets(conn) == {
        "lthr_bpm": 180,
        "lt_pace": "5:10/km",
        "tested_on": "2026-12-01",
        "easy_max_bpm": 160,
        "tempo_bpm": [171, 178],
    }
