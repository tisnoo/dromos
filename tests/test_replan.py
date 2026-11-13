import json
from datetime import date, timedelta

import pytest
from conftest import raw_run

from dromos import db, plan, replan, training
from dromos.garmin import normalise

START = date(2026, 11, 9)  # a Monday
TODAY = date(2026, 11, 13)  # Friday of week 1
TARGETS = [(20, 8, None), (24, 9, None), (26, 10, None), (20, 8, "cutback"), (28, 11, None)]


def make_plan(start: date = START) -> dict:
    """Four weeks of easy Mon/Wed + Sunday long run, a cutback, then a locked taper week."""
    weeks = []
    for n, (target, long_km, flag) in enumerate([*TARGETS, (18, 8, "taper")], start=1):
        monday = start + timedelta(weeks=n - 1)
        easy = (target - long_km) / 2
        weeks.append(
            {
                "number": n,
                "week_start": monday.isoformat(),
                "phase": "taper" if flag == "taper" else "base",
                "flag": flag,
                "target_km": target,
                "long_run_km": long_km,
                "key_session": f"Long run {long_km:g} km",
                "locked": flag == "taper",
                "sessions": [
                    {"date": monday.isoformat(), "type": "gym", "title": "Gym"},
                    {
                        "date": monday.isoformat(),
                        "type": "easy",
                        "title": f"Easy run {easy:g} km",
                        "target_km": easy,
                    },
                    {
                        "date": (monday + timedelta(days=2)).isoformat(),
                        "type": "easy",
                        "title": f"Easy run {easy:g} km",
                        "target_km": easy,
                    },
                    {
                        "date": (monday + timedelta(days=6)).isoformat(),
                        "type": "long",
                        "title": f"Long run {long_km:g} km",
                        "target_km": long_km,
                    },
                ],
            }
        )
    return {"weeks": weeks}


def add_runs(conn, runs: list[tuple[str, float]]) -> None:
    for i, (day, km) in enumerate(runs, start=1):
        raw = raw_run(i, f"{day} 08:00:00", distance_m=km * 1000, duration=km * 400)
        db.upsert_activity(conn, normalise(raw), raw)


@pytest.fixture
def loaded(conn):
    plan.import_plan(conn, make_plan(), reason="test")
    add_runs(conn, [("2026-10-31", 7.0), ("2026-11-09", 5.0), ("2026-11-11", 5.0)])
    return conn


def week(conn, number: int) -> dict:
    return next(w for w in training.all_weeks(conn) if w["week"] == number)


def test_context_load_and_pace(loaded):
    ctx = replan.context(loaded, TODAY)
    assert ctx.load_km == 10.0  # 10 km in the last 7 days; 17 km over 3 weeks averages less
    assert ctx.pace_s_per_km == 420  # 6:40/km median, 5% slower for long runs
    assert round(ctx.long_run_cap_km, 1) == 25.7


def test_load_has_a_floor_after_a_break(conn):
    assert replan.context(conn, TODAY).load_km == replan.MIN_LOAD_KM


def test_validate_flags_110_rule_and_three_hours(loaded):
    current = training.all_weeks(loaded)
    errors = replan.validate(current, [], replan.context(loaded, TODAY))
    # Longest run in the 30 days before 15 Nov is 7 km, so 8 km breaks the 110% rule.
    assert any("week 1: 8 km on 2026-11-15 breaks the 110% rule" in e for e in errors)

    slow = replan.Context(today=TODAY, runs=[(TODAY, 30.0)], pace_s_per_km=480)
    long_run = {**week(loaded, 2)}
    long_run["sessions"] = [
        {**s, "target_km": 23.0} for s in long_run["sessions"] if s["type"] in ("gym", "long")
    ]
    errors = replan.validate(current, [replan._with_totals(long_run)], slow)
    assert any("over 3 h at 8:00/km (max 22.5 km)" in e for e in errors)


def test_validate_protects_past_days_locked_weeks_and_originals(loaded):
    current = training.all_weeks(loaded)
    ctx = replan.context(loaded, TODAY)
    past = week(loaded, 1)
    past = {**past, "sessions": [s for s in past["sessions"] if s["date"] != "2026-11-09"]}
    assert any(
        "on or before 2026-11-13 can't change" in e for e in replan.validate(current, [past], ctx)
    )

    assert any("is locked" in e for e in replan.validate(current, [week(loaded, 6)], ctx))

    more = week(loaded, 2)
    more = replan._with_totals(
        {
            **more,
            "sessions": [
                {**s, "target_km": 9.0} if s["type"] == "easy" else s for s in more["sessions"]
            ],
        }
    )
    errors = replan.validate(current, [more], ctx)
    assert any("above the original 24 km" in e for e in errors)


def test_replan_grows_from_recent_load_and_passes_validation(loaded):
    current = training.all_weeks(loaded)
    ctx = replan.context(loaded, TODAY)
    weeks, notes = replan.replan(current, ctx)

    # This week's Sunday is only capped (7 km longest in 30 days → 7.7 km); taper untouched.
    assert [w["week"] for w in weeks] == [1, 2, 3, 4, 5]
    assert weeks[0]["long_run_km"] == 7.7
    assert weeks[0]["sessions"][-1]["title"] == "Long run 7.7 km"
    assert replan.validate(current, weeks, ctx) == []
    weeks = weeks[1:]
    targets = [w["target_km"] for w in weeks]
    assert targets[0] <= ctx.load_km * replan.WEEKLY_GROWTH + replan.WEEK_TOLERANCE_KM
    assert all(w["target_km"] <= w["original_target_km"] for w in weeks)
    assert targets[2] <= targets[1] * replan.CUTBACK_MAX + replan.WEEK_TOLERANCE_KM
    assert "week 1: 2026-11-15 capped at 7.7 km" in notes
    long_run = next(s for s in weeks[0]["sessions"] if s["type"] == "long")
    assert long_run["title"] == f"Long run {long_run['target_km']:g} km"
    assert weeks[0]["key_session"] == long_run["title"]


def test_replan_keeps_the_plan_when_load_is_on_track(conn):
    plan.import_plan(conn, make_plan(), reason="test")
    add_runs(conn, [("2026-11-01", 12.0), ("2026-11-08", 12.0), ("2026-11-12", 25.0)])
    weeks, _ = replan.replan(training.all_weeks(conn), replan.context(conn, TODAY))
    assert weeks == []


def test_propose_then_apply_writes_and_logs(loaded):
    weeks, notes = replan.replan(training.all_weeks(loaded), replan.context(loaded, TODAY))
    proposal = replan.propose(loaded, weeks, "behind plan", "claude", TODAY, notes)
    assert proposal["status"] == "proposed"
    assert proposal["changes"][1]["target_km"][0] == 24
    assert week(loaded, 2)["target_km"] == 24  # nothing written yet

    other = replan.propose(loaded, weeks, "another idea", "hermes", TODAY)
    applied = replan.apply(loaded, proposal["change_id"], TODAY)
    assert applied["weeks"] == [1, 2, 3, 4, 5]
    assert week(loaded, 2)["target_km"] == weeks[1]["target_km"]
    assert week(loaded, 2)["original_target_km"] == 24

    statuses = {c["change_id"]: c["status"] for c in replan.changes(loaded)}
    assert statuses[proposal["change_id"]] == "applied"
    assert statuses[other["change_id"]] == "rejected"  # validated against the old plan
    with pytest.raises(ValueError, match="is applied"):
        replan.apply(loaded, proposal["change_id"], TODAY)


def test_invalid_proposal_is_logged_as_rejected(loaded):
    proposal = replan.propose(loaded, [week(loaded, 6)], "shorter taper", "claude", TODAY)
    assert proposal["status"] == "rejected"
    stored = loaded.execute("SELECT diff FROM plan_changes WHERE id = ?", (proposal["change_id"],))
    assert "is locked" in json.loads(stored.fetchone()[0])["violations"][0]
    with pytest.raises(ValueError, match="is rejected"):
        replan.apply(loaded, proposal["change_id"], TODAY)


def test_apply_refuses_a_stale_proposal(loaded):
    weeks, _ = replan.replan(training.all_weeks(loaded), replan.context(loaded, TODAY))
    proposal = replan.propose(loaded, weeks, "behind plan", "claude", TODAY)
    loaded.execute("UPDATE plan_weeks SET key_session = 'edited by hand' WHERE number = 2")
    with pytest.raises(ValueError, match="changed since"):
        replan.apply(loaded, proposal["change_id"], TODAY)


def test_edits_only_block_on_violations_they_introduce(loaded):
    current = training.all_weeks(loaded)
    ctx = replan.context(loaded, TODAY)
    assert replan.validate(current, [], ctx)  # the fixture plan already breaks the 110% rule
    assert replan.new_violations(current, [], ctx) == []

    longer = week(loaded, 2)
    longer = replan._with_totals(
        {
            **longer,
            "sessions": [
                {**s, "target_km": 12.0 if s["type"] == "long" else 6.0}
                for s in longer["sessions"]
                if s["type"] != "gym"
            ],
        }
    )
    errors = replan.new_violations(current, [longer], ctx)
    assert any("12 km on 2026-11-22 breaks the 110% rule" in e for e in errors)


def test_edit_drops_a_session_and_keeps_past_days(loaded):
    current = training.all_weeks(loaded)
    edited = replan.edit_weeks(current, [{"week": 1, "sessions": []}], TODAY)[0]
    assert [s["date"] for s in edited["sessions"]] == ["2026-11-09", "2026-11-09", "2026-11-11"]
    assert edited["target_km"] == 12
    assert edited["long_run_km"] == 0

    proposal = replan.propose(loaded, [edited], "not running Sunday", "claude", TODAY)
    assert proposal["status"] == "proposed"
    assert proposal["changes"][0]["runs"] == [
        {"date": "2026-11-15", "type": "long", "km": [8.0, None]}
    ]
    replan.apply(loaded, proposal["change_id"], TODAY)
    assert week(loaded, 1)["target_km"] == 12
