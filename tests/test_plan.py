import json

import pytest

from dromos import plan


def make_plan(weeks: int = 2) -> dict:
    return {
        "weeks": [
            {
                "number": n,
                "week_start": f"2026-11-{9 + 7 * (n - 1):02d}",
                "phase": "base",
                "flag": None,
                "target_km": 20,
                "long_run_km": 8,
                "key_session": "Easy run 4 km",
                "locked": False,
                "sessions": [
                    {
                        "date": f"2026-11-{9 + 7 * (n - 1):02d}",
                        "type": "easy",
                        "title": "Easy run 5 km",
                        "target_km": 5,
                        "target_zone": "Z1",
                    },
                    {"date": f"2026-11-{9 + 7 * (n - 1):02d}", "type": "gym", "title": "Gym"},
                ],
            }
            for n in range(1, weeks + 1)
        ]
    }


def test_import_plan_loads_weeks_sessions_and_logs_change(conn):
    plan.import_plan(conn, make_plan(), reason="test import")

    assert conn.execute("SELECT COUNT(*) FROM plan_weeks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 4
    change = conn.execute("SELECT * FROM plan_changes").fetchone()
    assert change["status"] == "applied"
    assert json.loads(change["diff"])["sessions"] == 4


def test_import_refuses_to_overwrite_without_replace(conn):
    plan.import_plan(conn, make_plan(), reason="first")
    with pytest.raises(plan.PlanError, match="already loaded"):
        plan.import_plan(conn, make_plan(), reason="second")

    plan.import_plan(conn, make_plan(weeks=1), reason="second", replace=True)
    assert conn.execute("SELECT COUNT(*) FROM plan_weeks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM plan_changes").fetchone()[0] == 2


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda p: p["weeks"][0].update(week_start="2026-11-10"), "not a Monday"),
        (lambda p: p["weeks"][1].update(week_start="2026-11-23"), "consecutive"),
        (lambda p: p["weeks"][1].update(number=3), "numbered"),
        (lambda p: p["weeks"][0].update(phase="peak"), "phase"),
        (lambda p: p["weeks"][0]["sessions"][0].update(date="2026-11-16"), "outside the week"),
        (lambda p: p["weeks"][0]["sessions"][0].update(type="swim"), "session type"),
    ],
)
def test_validate_rejects_bad_plans(conn, edit, message):
    bad = make_plan()
    edit(bad)
    with pytest.raises(plan.PlanError, match=message):
        plan.import_plan(conn, bad, reason="bad")
    assert not plan.has_plan(conn)
