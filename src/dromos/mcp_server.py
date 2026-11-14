"""MCP endpoint: the only way AI clients read and change training data.

Plan changes go through propose → validate → apply (`replan.py`); never add a tool that
writes to the plan without the validator.
"""

from datetime import date, timedelta
from typing import Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from dromos import db, replan, training
from dromos.auth import HERMES_CLIENT_ID, DromosOAuthProvider

INSTRUCTIONS = """\
Dromos holds the user's marathon training plan and the runs their Garmin watch recorded
(synced hourly). Use it to answer questions about training progress and to compare what was
planned with what was actually run.

- Plan weeks run Monday to Sunday. Days without sessions are rest days. Taper weeks are locked.
- Zones are the three-zone research model: Z1 below LT1 (easy), Z2 between LT1 and LT2
  (marathon pace, tempo), Z3 above LT2. Garmin's hr_zone_minutes use Garmin's 5 zones.
- Gym sessions are in the plan, but only runs and sleep are recorded so far.
- Sleep is per night, dated by the morning it ended, with Garmin's sleep score (0-100),
  stages, resting HR, overnight HRV and Body Battery recovered. Low scores, a falling HRV or
  a resting HR well above normal for several days are signs of fatigue: suggest easing off.
- Changing the plan is propose → apply. propose_replan rebuilds the weeks after today from
  recent load; propose_plan_change edits specific weeks (e.g. moving or dropping sessions).
  Both are checked against the plan's rules; show the user the proposal (and any violations)
  and only call apply_plan_change once they agree. Days up to today, taper weeks and the
  race never change, and missed km are never added back.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
PROPOSE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
APPLY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


class Session(BaseModel):
    date: date
    type: Literal["easy", "tempo", "marathon_pace", "interval", "long", "gym", "mobility"]
    title: str = Field(description='Include the distance for runs, e.g. "Easy run 5 km"')
    target_km: float | None = Field(None, description="Required for runs")
    target_zone: str | None = Field(None, description="Z1, Z2 or Z3")
    notes: str | None = None


class WeekEdit(BaseModel):
    week: int
    sessions: list[Session] = Field(
        description="The week's full list of sessions from tomorrow on, gym included. "
        "Days left out become rest days; days up to today are kept as they are."
    )
    flag: Literal["cutback", "peak"] | None = Field(None, description="Omit to keep the current")
    key_session: str | None = Field(None, description="Omit to keep the current")


def _source() -> str:
    token = get_access_token()
    return "hermes" if token and token.client_id == HERMES_CLIENT_ID else "claude"


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
            "last_night": next(iter(_query(training.sleep, today, today)), None),
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
    def get_sleep(start_date: date | None = None, end_date: date | None = None) -> dict[str, Any]:
        """Nights recorded by Garmin between two dates (inclusive), by the morning they ended,
        oldest first: sleep score, stages, resting HR, HRV, Body Battery. Defaults to the
        last 14 days."""
        end = end_date or date.today()
        start = start_date or end - timedelta(days=13)
        return {"nights": _query(training.sleep, start, end)}

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

    @mcp.tool(annotations=READ_ONLY)
    def get_plan_changes(limit: int = 10) -> dict[str, Any]:
        """Recent plan changes, newest first: proposals, applied and rejected changes, with
        what changed per week and any rule violations."""
        return {"changes": _query(replan.changes, limit)}

    @mcp.tool(annotations=PROPOSE)
    def propose_replan(reason: str) -> dict[str, Any]:
        """Rebuild every unlocked week after today from recent load and the plan's rules.
        Doesn't change the plan: returns a proposal to show the user and apply if they agree.
        `reason`: why, in the user's words (e.g. "missed the long run, sick this week")."""
        today = date.today()
        conn = db.connect()
        try:
            ctx = replan.context(conn, today)
            weeks, notes = replan.replan(training.all_weeks(conn), ctx)
            if not weeks:
                return {"status": "unchanged", "notes": ["The plan already fits recent load"]}
            notes = [
                f"recent load {ctx.load_km} km/week; long-run pace "
                f"{training.fmt_pace(ctx.pace_s_per_km)}",
                *notes,
            ]
            return replan.propose(conn, weeks, reason, _source(), today, notes)
        finally:
            conn.close()

    @mcp.tool(annotations=PROPOSE)
    def propose_plan_change(weeks: list[WeekEdit], reason: str) -> dict[str, Any]:
        """Propose edits to specific weeks: move, add, shorten or drop sessions, or set a
        week's flag. Week targets follow from the sessions. Doesn't change the plan: returns
        the proposal and any rule violations; fix those and propose again if needed."""
        today = date.today()
        conn = db.connect()
        try:
            edits = [
                {
                    "week": w.week,
                    "sessions": [
                        {**s.model_dump(), "date": s.date.isoformat()} for s in w.sessions
                    ],
                    **{
                        k: getattr(w, k) for k in ("flag", "key_session") if k in w.model_fields_set
                    },
                }
                for w in weeks
            ]
            proposed = replan.edit_weeks(training.all_weeks(conn), edits, today)
            return replan.propose(conn, proposed, reason, _source(), today)
        finally:
            conn.close()

    @mcp.tool(annotations=APPLY)
    def apply_plan_change(change_id: int) -> dict[str, Any]:
        """Write a proposed change to the plan, once the user has agreed to it. Checks it
        again first; other open proposals are then discarded."""
        return _query(replan.apply, change_id, date.today())

    return mcp
