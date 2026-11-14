from datetime import date, datetime

from conftest import raw_sleep

from dromos.garmin import normalise, normalise_sleep, sleep_summary

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


def test_normalise_sleep():
    raw = raw_sleep("2026-11-13", score=81)
    night = normalise_sleep(raw)
    assert night.calendar_date == date(2026, 11, 13)
    assert (night.score, night.quality, night.duration_s) == (81, "GOOD", 27000)
    assert (night.resting_hr, night.avg_overnight_hrv, night.hrv_status) == (55, 60.0, "BALANCED")
    assert "sleepLevels" not in sleep_summary(raw)


def test_normalise_sleep_without_a_night():
    assert normalise_sleep({"dailySleepDTO": {"calendarDate": "2026-11-13"}}) is None
    assert normalise_sleep({}) is None
