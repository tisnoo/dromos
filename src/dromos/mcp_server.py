"""MCP endpoint: the only way AI clients read (and, from Phase 5, change) training data.

Read-only for now. Plan changes will go through propose → validate → apply; never add a
tool that writes to the plan without the validator.
"""

from datetime import date, timedelta
from typing import Any

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from dromos import db, training
from dromos.auth import DromosOAuthProvider

INSTRUCTIONS = """\
Dromos holds the user's marathon training plan and the runs their Garmin watch recorded
(synced hourly). Use it to answer questions about training progress and to compare what was
planned with what was actually run.

- Plan weeks run Monday to Sunday. Days without sessions are rest days. Taper weeks are locked.
- Zones are the three-zone research model: Z1 below LT1 (easy), Z2 between LT1 and LT2
  (marathon pace, tempo), Z3 above LT2. Garmin's hr_zone_minutes use Garmin's 5 zones.
- Gym sessions are in the plan, but only runs are recorded so far.
- Changing the plan isn't available yet; explain what you'd change instead.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _query(fn, *args) -> Any:
    conn = db.connect()
    try:
        return fn(conn, *args)
    finally:
        conn.close()


def create_mcp(base_url: str, provider: DromosOAuthProvider) -> MCPServer:
    mcp = MCPServer(
        name="Dromos",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base_url,
            resource_server_url=f"{base_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=False,  # one resource only; Hermes' token has none
        ),
    )

    @mcp.tool(annotations=READ_ONLY)
    def get_training_status() -> dict[str, Any]:
        """Today's date, the current plan week, days until the race, and this week's plan
        vs actual so far. A good first call in a conversation."""
        today = date.today()
        race = _query(training.race_date)
        week = _query(training.week_number_for, today)
        return {
            "today": today.isoformat(),
            "race_date": race.isoformat() if race else None,
            "days_to_race": (race - today).days if race else None,
            "current_week": week,
            "this_week": _query(training.compare_week, week, today) if week else None,
        }

    @mcp.tool(annotations=READ_ONLY)
    def get_activities(
        start_date: date | None = None, end_date: date | None = None
    ) -> dict[str, Any]:
        """Runs recorded by Garmin between two dates (inclusive), oldest first.
        Defaults to the last 28 days."""
        end = end_date or date.today()
        start = start_date or end - timedelta(days=27)
        return {"runs": _query(training.activities, start, end)}

    @mcp.tool(annotations=READ_ONLY)
    def get_plan(start_date: date | None = None, end_date: date | None = None) -> dict[str, Any]:
        """Planned weeks overlapping two dates, with each week's sessions.
        Defaults to this week and the next three."""
        start = start_date or date.today()
        end = end_date or start + timedelta(weeks=4) - timedelta(days=1)
        return {"weeks": _query(training.plan, start, end)}

    @mcp.tool(annotations=READ_ONLY)
    def compare_plan_vs_actual(week: int | None = None) -> dict[str, Any]:
        """Planned vs actual for one plan week (1 = first week), day by day, with km totals,
        longest run and heart-rate zone minutes. Defaults to the current week."""
        today = date.today()
        number = week or _query(training.week_number_for, today)
        if number is None:
            raise ValueError("Today is outside the plan; pass a week number")
        return _query(training.compare_week, number, today)

    return mcp
