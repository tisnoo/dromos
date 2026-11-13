from datetime import datetime

from dromos.garmin import normalise

RAW = {
    "activityId": 123456789,
    "startTimeLocal": "2026-11-10 07:15:00",
    "activityType": {"typeKey": "running"},
    "distance": 10000.0,
    "duration": 3300.0,
    "averageHR": 148.0,
    "maxHR": 171.0,
    "hrTimeInZone_1": 120.0,
    "hrTimeInZone_2": 2700.0,
    "hrTimeInZone_3": 480.0,
}


def test_normalise_run():
    run = normalise(RAW)
    assert run.garmin_id == 123456789
    assert run.start_time == datetime(2026, 11, 10, 7, 15)
    assert run.distance_km == 10.0
    assert run.avg_pace_s_per_km == 330.0
    assert run.avg_hr == 148.0
    assert run.hr_zone_seconds == {1: 120.0, 2: 2700.0, 3: 480.0}


def test_normalise_handles_missing_distance_and_hr():
    run = normalise({**RAW, "distance": None, "averageHR": None, "hrTimeInZone_1": None})
    assert run.distance_km == 0.0
    assert run.avg_pace_s_per_km is None
    assert run.avg_hr is None
    assert 1 not in run.hr_zone_seconds
