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


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()
