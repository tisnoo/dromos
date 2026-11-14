"""Garmin Connect access: login with a persisted token store, fetch and normalise runs and sleep."""

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from garminconnect import Garmin

DEFAULT_TOKENSTORE = "~/.dromos/garmin-tokens"


@dataclass(frozen=True)
class Run:
    garmin_id: int
    start_time: datetime
    activity_type: str
    distance_km: float
    duration_s: float
    avg_pace_s_per_km: float | None
    avg_hr: float | None
    max_hr: float | None
    hr_zone_seconds: dict[int, float]


@dataclass(frozen=True)
class Sleep:
    calendar_date: date  # the morning the night ended
    score: int | None
    quality: str | None  # Garmin's qualifier for the score: EXCELLENT, GOOD, FAIR, POOR
    duration_s: float
    deep_s: float | None
    light_s: float | None
    rem_s: float | None
    awake_s: float | None
    awake_count: int | None
    avg_sleep_stress: float | None
    resting_hr: float | None
    avg_overnight_hrv: float | None
    hrv_status: str | None
    body_battery_change: float | None
    feedback: str | None


def connect(tokenstore: str | None = None, interactive: bool = True) -> Garmin:
    """Log in using saved tokens; fall back to GARMIN_EMAIL/GARMIN_PASSWORD (prompts for MFA).

    A credential login writes fresh tokens to the token store, so later runs
    (and the server) only need the token store. With interactive=False (scheduled
    jobs) only the token store is tried, so a missing or expired store fails fast
    instead of waiting on an MFA prompt.
    """
    path = str(Path(tokenstore or os.getenv("GARMINTOKENS") or DEFAULT_TOKENSTORE).expanduser())
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if interactive:
        client = Garmin(
            email=os.getenv("GARMIN_EMAIL"),
            password=os.getenv("GARMIN_PASSWORD"),
            prompt_mfa=lambda: input("Garmin MFA code: "),
        )
    else:
        client = Garmin()
    client.login(tokenstore=path)
    return client


def fetch_raw_runs(client: Garmin, start: date, end: date) -> list[dict[str, Any]]:
    return client.get_activities_by_date(
        start.isoformat(), end.isoformat(), activitytype="running", sortorder="asc"
    )


def normalise(raw: dict[str, Any]) -> Run:
    distance_km = (raw.get("distance") or 0.0) / 1000
    duration_s = raw.get("duration") or 0.0
    return Run(
        garmin_id=int(raw["activityId"]),
        start_time=datetime.fromisoformat(raw["startTimeLocal"]),
        activity_type=raw.get("activityType", {}).get("typeKey", "unknown"),
        distance_km=round(distance_km, 3),
        duration_s=duration_s,
        avg_pace_s_per_km=round(duration_s / distance_km, 1) if distance_km else None,
        avg_hr=raw.get("averageHR"),
        max_hr=raw.get("maxHR"),
        hr_zone_seconds={
            zone: raw[f"hrTimeInZone_{zone}"]
            for zone in range(1, 6)
            if raw.get(f"hrTimeInZone_{zone}") is not None
        },
    )


def fetch_raw_sleep(client: Garmin, day: date) -> dict[str, Any]:
    return client.get_sleep_data(day.isoformat())


# Top-level fields kept alongside dailySleepDTO; the rest are large per-minute series.
SLEEP_SUMMARY_FIELDS = ("restingHeartRate", "avgOvernightHrv", "hrvStatus", "bodyBatteryChange")


def sleep_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """The part of a sleep response worth storing."""
    return {
        "dailySleepDTO": raw.get("dailySleepDTO") or {},
        **{k: raw.get(k) for k in SLEEP_SUMMARY_FIELDS},
    }


def normalise_sleep(raw: dict[str, Any]) -> Sleep | None:
    """None when there is no recorded night (watch not worn, or not synced yet)."""
    dto = raw.get("dailySleepDTO") or {}
    if not dto.get("calendarDate") or not dto.get("sleepTimeSeconds"):
        return None
    overall = (dto.get("sleepScores") or {}).get("overall") or {}
    return Sleep(
        calendar_date=date.fromisoformat(dto["calendarDate"]),
        score=overall.get("value"),
        quality=overall.get("qualifierKey"),
        duration_s=dto["sleepTimeSeconds"],
        deep_s=dto.get("deepSleepSeconds"),
        light_s=dto.get("lightSleepSeconds"),
        rem_s=dto.get("remSleepSeconds"),
        awake_s=dto.get("awakeSleepSeconds"),
        awake_count=dto.get("awakeCount"),
        avg_sleep_stress=dto.get("avgSleepStress"),
        resting_hr=raw.get("restingHeartRate"),
        avg_overnight_hrv=raw.get("avgOvernightHrv"),
        hrv_status=raw.get("hrvStatus"),
        body_battery_change=raw.get("bodyBatteryChange"),
        feedback=dto.get("sleepScoreFeedback"),
    )
