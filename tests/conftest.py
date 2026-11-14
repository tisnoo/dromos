import pytest

from dromos import db


def raw_run(garmin_id: int, start: str, distance_m: float = 10000.0, **extra) -> dict:
    """A minimal activity dict shaped like Garmin's activity list JSON."""
    return {
        "activityId": garmin_id,
        "startTimeLocal": start,
        "activityType": {"typeKey": "running"},
        "distance": distance_m,
        "duration": 3300.0,
        "averageHR": 148.0,
        "maxHR": 171.0,
        "hrTimeInZone_2": 2700.0,
        **extra,
    }


def raw_sleep(day: str, score: int = 80, **extra) -> dict:
    """A minimal sleep response shaped like Garmin's get_sleep_data JSON."""
    return {
        "dailySleepDTO": {
            "calendarDate": day,
            "sleepTimeSeconds": 27000,
            "deepSleepSeconds": 5400,
            "lightSleepSeconds": 16200,
            "remSleepSeconds": 5400,
            "awakeSleepSeconds": 600,
            "awakeCount": 1,
            "avgSleepStress": 15.0,
            "sleepScoreFeedback": "POSITIVE_LONG_AND_DEEP",
            "sleepScores": {"overall": {"value": score, "qualifierKey": "GOOD"}},
        },
        "sleepLevels": [{"startGMT": "...", "activityLevel": 1.0}],
        "restingHeartRate": 55,
        "avgOvernightHrv": 60.0,
        "hrvStatus": "BALANCED",
        "bodyBatteryChange": 50,
        "hrvSummary": {
            "weeklyAvg": 58,
            "lastNightAvg": 60,
            "status": "BALANCED",
            "baseline": {"lowUpper": 45, "balancedLow": 50, "balancedUpper": 80},
        },
        **extra,
    }


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()
