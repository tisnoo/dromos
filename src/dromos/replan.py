"""Re-planning: the rules validator, deterministic `replan()`, and propose → validate → apply.

A change is a list of replacement weeks (same shape as `training.plan()` weeks). `propose()`
validates it against the current plan and recent runs (only violations the change
introduces count, so an edit isn't blocked by older ones) and logs it in `plan_changes`, as
'proposed' or, with the violations, 'rejected'. Only `apply()` writes to the plan, and it
validates again first, because runs and today's date may have moved on since the proposal.

Rules checked by `validate()`:

- Locked weeks (taper, race), race sessions and days up to today never change.
- 110% rule: no planned run exceeds 110% of the longest run in the 30 days before it. Runs up
  to today count as recorded, later days count as planned.
- Long runs are capped at 3 h on feet, at the pace of recent runs.
- No week is planned above its original target, so missed km are never added back.
- A week's target is at most 15% above the higher of the two normal (non-cutback) weeks
  before it, where recent load stands in for the weeks up to today: the last 7 days, or the
  3-week average if higher, at least 10 km. So one lighter week doesn't hold back the next.
- A cutback at least every 4th week, at most 85% of the week before it.

`replan()` rebuilds every unlocked week after today from recent load by the same rules: each
week grows from the last one until it reaches the original plan again. If it can't before the
taper, the peak ends up lower; the build is never compressed.
"""

import json
import math
import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from dromos import training
from dromos.plan import SESSION_TYPES
from dromos.training import RUN_SESSION_TYPES

SINGLE_RUN_LIMIT = 1.10
SINGLE_RUN_WINDOW_DAYS = 30
NO_HISTORY_LIMIT_KM = 5.0  # longest run allowed when nothing was run in the window
LONG_RUN_MAX_S = 3 * 3600
PACE_WINDOW_DAYS = 42
LONG_RUN_PACE_FACTOR = 1.05  # long runs go a little slower than the recent median
DEFAULT_PACE_S_PER_KM = 450.0  # 7:30/km when there are no recent runs to go by
WEEKLY_GROWTH = 1.15
MIN_LOAD_KM = 10.0  # restart from here after a break, rather than from zero
CUTBACK_MAX = 0.85
MAX_WEEKS_WITHOUT_CUTBACK = 3
EDITABLE_FLAGS = {None, "cutback", "peak"}
TOLERANCE_KM = 0.05
WEEK_TOLERANCE_KM = 0.25  # runs are rounded to 0.5 km, so week totals land within this
SESSION_FIELDS = ("date", "type", "title", "target_km", "target_zone", "notes")


@dataclass
class Context:
    today: date
    runs: list[tuple[date, float]]  # recorded runs up to today, by plan date
    pace_s_per_km: float  # expected long-run pace

    @property
    def load_km(self) -> float:
        """Recent weekly load: the last 7 days, or the 3-week average if that is higher."""
        last_7 = sum(km for day, km in self.runs if (self.today - day).days < 7)
        last_21 = sum(km for day, km in self.runs if (self.today - day).days < 21)
        return round(max(last_7, last_21 / 3, MIN_LOAD_KM), 2)

    @property
    def long_run_cap_km(self) -> float:
        return LONG_RUN_MAX_S / self.pace_s_per_km


def context(conn: sqlite3.Connection, today: date) -> Context:
    since = today - timedelta(days=max(SINGLE_RUN_WINDOW_DAYS, PACE_WINDOW_DAYS))
    runs = training.activities(conn, since, today)
    paces = [
        r["duration_min"] * 60 / r["distance_km"]
        for r in runs
        if r["distance_km"] >= 3
        and r["duration_min"] * 60 / r["distance_km"] <= 720  # skip walks
        and (today - date.fromisoformat(r["date"])).days < PACE_WINDOW_DAYS
    ]
    pace = statistics.median(paces) * LONG_RUN_PACE_FACTOR if paces else DEFAULT_PACE_S_PER_KM
    return Context(
        today=today,
        runs=[(date.fromisoformat(r["date"]), r["distance_km"]) for r in runs],
        pace_s_per_km=round(pace),
    )


def _is_run(session: dict[str, Any]) -> bool:
    return session["type"] in RUN_SESSION_TYPES


def _km(session: dict[str, Any]) -> float:
    return session.get("target_km") or 0.0


def _fmt_km(km: float) -> str:
    return f"{round(km, 1):g}"


def _snapshot(week: dict[str, Any]) -> dict[str, Any]:
    """The parts of a week a change may touch, for comparing and logging."""
    return {
        "week": week["week"],
        "start": week["start"],
        "flag": week["flag"],
        "target_km": week["target_km"],
        "long_run_km": week["long_run_km"],
        "key_session": week["key_session"],
        "sessions": [{k: s.get(k) for k in SESSION_FIELDS} for s in week["sessions"]],
    }


def _with_totals(week: dict[str, Any]) -> dict[str, Any]:
    """Derive target_km and long_run_km from the week's run sessions."""
    runs = [s for s in week["sessions"] if _is_run(s)]
    long_runs = [_km(s) for s in runs if s["type"] in ("long", "race")]
    return {
        **week,
        "target_km": round(sum(_km(s) for s in runs), 2),
        "long_run_km": max(long_runs, default=0.0),
    }


def _single_run_limit(day: date, ctx: Context, planned: list[tuple[date, float]]) -> float:
    start = day - timedelta(days=SINGLE_RUN_WINDOW_DAYS)
    recorded = [km for d, km in ctx.runs if start <= d < day]
    future = [km for d, km in planned if ctx.today < d < day and d >= start]
    longest = max(recorded + future, default=0.0)
    return longest * SINGLE_RUN_LIMIT if longest else NO_HISTORY_LIMIT_KM


def _planned_runs(weeks: list[dict[str, Any]], today: date) -> list[tuple[date, float]]:
    return sorted(
        (date.fromisoformat(s["date"]), _km(s))
        for w in weeks
        for s in w["sessions"]
        if _is_run(s) and s["date"] > today.isoformat()
    )


def _past_sessions(week: dict[str, Any], today: str) -> list[dict[str, Any]]:
    return [{k: s.get(k) for k in SESSION_FIELDS} for s in week["sessions"] if s["date"] <= today]


def _violations(
    current: list[dict[str, Any]], proposed: list[dict[str, Any]], ctx: Context
) -> list[tuple[str, str]]:
    """(key, message) per violation; the key names the rule and session or week it is about."""
    today = ctx.today.isoformat()
    by_number = {w["week"]: w for w in current}
    errors: list[tuple[str, str]] = []

    def add(key: str | None, message: str) -> None:
        errors.append((key or message, message))

    for week in proposed:
        where = f"week {week['week']}"
        old = by_number.get(week["week"])
        if old is None:
            add(None, f"{where} is not in the plan")
            continue
        if old["locked"]:
            add(None, f"{where} is locked (taper and race weeks never change)")
        if week["flag"] != old["flag"] and week["flag"] not in EDITABLE_FLAGS:
            add(None, f"{where}: flag can only be one of cutback, peak or none")
        if _past_sessions(old, today) != _past_sessions(week, today):
            add(None, f"{where}: sessions on or before {today} can't change")
        old_races = [s for s in old["sessions"] if s["type"] == "race"]
        if old_races != [s for s in week["sessions"] if s["type"] == "race"]:
            add(None, f"{where}: race sessions can't change")
        for s in week["sessions"]:
            if not old["start"] <= s["date"] <= old["end"]:
                add(None, f"{where}: session on {s['date']} is outside the week")
            if s["type"] not in SESSION_TYPES:
                add(None, f"{where}: unknown session type {s['type']!r}")
            if not s.get("title"):
                add(None, f"{where}: session on {s['date']} has no title")
            if _is_run(s) and not _km(s) > 0:
                add(None, f"{where}: run on {s['date']} needs a target_km")
        if week["target_km"] > (old["original_target_km"] or 0) + WEEK_TOLERANCE_KM:
            add(
                None,
                f"{where}: {_fmt_km(week['target_km'])} km is above the original "
                f"{_fmt_km(old['original_target_km'])} km (missed km are never added back)",
            )
    if errors:
        return errors

    replaced = {w["week"]: w for w in proposed}
    weeks = [{**w, **replaced.get(w["week"], {})} for w in current]
    planned = _planned_runs(weeks, ctx.today)

    # Single runs, in every unlocked week, from tomorrow on.
    for week in weeks:
        if week["locked"]:
            continue
        for s in week["sessions"]:
            if not _is_run(s) or s["date"] <= today or s["type"] == "race":
                continue
            day = date.fromisoformat(s["date"])
            limit = _single_run_limit(day, ctx, planned)
            if _km(s) > limit + TOLERANCE_KM:
                add(
                    f"110%:{s['date']}",
                    f"week {week['week']}: {_fmt_km(_km(s))} km on {s['date']} breaks the "
                    f"110% rule (max {limit:.1f} km)",
                )
            if s["type"] == "long" and _km(s) > ctx.long_run_cap_km + TOLERANCE_KM:
                add(
                    f"3h:{s['date']}",
                    f"week {week['week']}: {_fmt_km(_km(s))} km long run on {s['date']} is "
                    f"over 3 h at {training.fmt_pace(ctx.pace_s_per_km)} "
                    f"(max {ctx.long_run_cap_km:.1f} km)",
                )

    # Weekly volume and cutbacks, for unlocked weeks that start after today.
    normal, previous = [ctx.load_km], ctx.load_km
    streak = 0
    for week in weeks:
        resets = week["flag"] in ("cutback", "taper", "race") or week["phase"] == "taper"
        streak = 0 if resets else streak + 1
        if week["start"] <= today or week["locked"]:
            continue
        where = f"week {week['week']}"
        if week["target_km"] > (week["original_target_km"] or 0) + WEEK_TOLERANCE_KM:
            add(
                f"original:{where}",
                f"{where}: {_fmt_km(week['target_km'])} km is above the original plan",
            )
        if week["flag"] == "cutback":
            if week["target_km"] > previous * CUTBACK_MAX + WEEK_TOLERANCE_KM:
                add(
                    f"cutback:{where}",
                    f"{where}: a cutback week must be at most 85% of the week before",
                )
        elif week["target_km"] > max(normal[-2:]) * WEEKLY_GROWTH + WEEK_TOLERANCE_KM:
            add(
                f"growth:{where}",
                f"{where}: {_fmt_km(week['target_km'])} km is more than 15% above "
                f"{_fmt_km(round(max(normal[-2:]), 1))} km (grow gradually)",
            )
        if streak > MAX_WEEKS_WITHOUT_CUTBACK:
            add(f"streak:{where}", f"{where}: needs a cutback week at least every 4th week")
        previous = week["target_km"]
        if week["flag"] != "cutback":
            normal.append(week["target_km"])
    return errors


def validate(
    current: list[dict[str, Any]], proposed: list[dict[str, Any]], ctx: Context
) -> list[str]:
    """Check proposed replacement weeks against the rules. Returns the violations."""
    return [message for _, message in _violations(current, proposed, ctx)]


def new_violations(
    current: list[dict[str, Any]], proposed: list[dict[str, Any]], ctx: Context
) -> list[str]:
    """Violations a change introduces: new ones, and old ones on a run or week it changes.
    Others the plan already has don't block small edits; `replan()` clears those."""
    by_number = {w["week"]: w for w in current}
    touched = set()
    for week in proposed:
        old = by_number.get(week["week"])
        if old is None:
            continue
        if week["target_km"] != old["target_km"]:
            touched.add(f"week {week['week']}")
        old_km = {s["date"]: _km(s) for s in old["sessions"] if _is_run(s)}
        touched |= {
            s["date"] for s in week["sessions"] if _is_run(s) and old_km.get(s["date"]) != _km(s)
        }
    existing = {key for key, _ in _violations(current, [], ctx)}
    return [
        message
        for key, message in _violations(current, proposed, ctx)
        if key not in existing or key.split(":", 1)[1] in touched
    ]


def _floor_half(km: float) -> float:
    return math.floor(km * 2 + 1e-9) / 2


def _retitle(title: str, km: float) -> str:
    return re.sub(r"\d+(?:\.\d+)? km", f"{_fmt_km(km)} km", title, count=1)


def replan(current: list[dict[str, Any]], ctx: Context) -> tuple[list[dict[str, Any]], list[str]]:
    """Rebuild every unlocked week after today from recent load. Returns (changed weeks, notes).

    Weekly targets grow at most 15% a week from recent load and never pass the original plan;
    cutback weeks keep their original drop. Within a week the long run keeps its original
    share and the other runs split the rest as they do now. Runs are rounded to 0.5 km and
    capped by the 110% rule and the 3 h limit; capped km are dropped, not moved.
    """
    today = ctx.today.isoformat()
    by_number = {w["week"]: w for w in current}
    planned = _planned_runs([w for w in current if w["locked"]], ctx.today)
    normal, previous = [ctx.load_km], ctx.load_km
    changed, notes = [], []

    # This week keeps its sessions; only runs still to come are capped by the rules.
    for week in current:
        if not week["start"] <= today <= week["end"] or week["locked"]:
            continue
        sessions = []
        for s in week["sessions"]:
            if _is_run(s) and s["date"] > today and s["type"] != "race":
                day = date.fromisoformat(s["date"])
                cap = _single_run_limit(day, ctx, planned)
                if s["type"] == "long":
                    cap = min(cap, ctx.long_run_cap_km)
                if _km(s) > cap + TOLERANCE_KM:
                    km = math.floor(cap * 10) / 10
                    notes.append(f"week {week['week']}: {s['date']} capped at {_fmt_km(km)} km")
                    s = {**s, "target_km": km, "title": _retitle(s["title"], km)}
                planned.append((day, _km(s)))
                planned.sort()
            sessions.append(s)
        new = _with_totals({**week, "sessions": sessions})
        if _snapshot(new) != _snapshot(week):
            changed.append(new)

    for week in current:
        if week["start"] <= today or week["locked"]:
            continue
        original = week["original_target_km"] or 0
        prior = by_number.get(week["week"] - 1)
        if week["flag"] == "cutback":
            prior_original = (prior or {}).get("original_target_km") or 0
            ratio = min(original / prior_original, CUTBACK_MAX) if prior_original else CUTBACK_MAX
            target = min(original, previous * ratio)
        else:
            target = min(original, max(normal[-2:]) * WEEKLY_GROWTH)
        week_notes = []
        if target < original - TOLERANCE_KM:
            week_notes.append(f"{_fmt_km(round(target, 1))} km instead of {_fmt_km(original)}")

        runs = [s for s in week["sessions"] if _is_run(s)]
        long_share = (week["original_long_run_km"] or 0) / original if original else 0
        has_long = any(s["type"] == "long" for s in runs)
        long_km = target * long_share if has_long else 0
        others = [s for s in runs if s["type"] != "long"]
        other_total = sum(_km(s) for s in others)
        desired = {}
        for s in runs:
            if s["type"] == "long":
                desired[id(s)] = long_km
            elif other_total:
                desired[id(s)] = (target - long_km) * _km(s) / other_total
            else:
                desired[id(s)] = (target - long_km) / len(others)

        # Cap each run in date order, round down to 0.5 km, then round back up the runs
        # with the largest remainders while the week stays within its (capped) total.
        km, caps, wanted, chain = {}, {}, {}, list(planned)
        for s in sorted(runs, key=lambda s: s["date"]):
            day = date.fromisoformat(s["date"])
            wanted[id(s)] = desired[id(s)]
            cap = _single_run_limit(day, ctx, chain)
            if wanted[id(s)] > cap:
                week_notes.append(f"{s['date']} capped at {cap:.1f} km by the 110% rule")
            if s["type"] == "long" and min(wanted[id(s)], cap) > ctx.long_run_cap_km:
                cap = ctx.long_run_cap_km
                week_notes.append(
                    f"long run capped at 3 h (~{cap:.1f} km at "
                    f"{training.fmt_pace(ctx.pace_s_per_km)})"
                )
            caps[id(s)] = cap
            # Capped runs round down to 0.1 km so the 110% rule doesn't lose ground weekly.
            km[id(s)] = (
                _floor_half(wanted[id(s)]) if wanted[id(s)] <= cap else math.floor(cap * 10) / 10
            )
            chain.append((day, km[id(s)]))
        total = sum(min(wanted[i], caps[i]) for i in km)
        while sum(km.values()) + 0.5 <= total + WEEK_TOLERANCE_KM:
            room = [s for s in runs if km[id(s)] + 0.5 <= caps[id(s)] + TOLERANCE_KM]
            if not room:
                break
            bump = max(room, key=lambda s: wanted[id(s)] - km[id(s)])
            km[id(bump)] += 0.5

        sessions = []
        for s in week["sessions"]:
            if _is_run(s):
                new_km = km[id(s)]
                planned.append((date.fromisoformat(s["date"]), new_km))
                s = {**s, "target_km": new_km, "title": _retitle(s["title"], new_km)}
            sessions.append(dict(s))
        planned.sort()

        titles = {s["title"]: n["title"] for s, n in zip(week["sessions"], sessions, strict=True)}
        new = _with_totals(
            {
                **week,
                "key_session": titles.get(week["key_session"], week["key_session"]),
                "sessions": sessions,
            }
        )
        if _snapshot(new) != _snapshot(week):
            changed.append(new)
            notes += [f"week {week['week']}: {n}" for n in week_notes]
        previous = new["target_km"]
        if week["flag"] != "cutback":
            normal.append(new["target_km"])
    return changed, notes


def edit_weeks(
    current: list[dict[str, Any]], edits: list[dict[str, Any]], today: date
) -> list[dict[str, Any]]:
    """Turn edits ({week, sessions from tomorrow on, optional flag/key_session}) into weeks.

    Sessions up to today are kept as they are; targets are derived from the sessions.
    """
    by_number = {w["week"]: w for w in current}
    weeks = []
    for edit in edits:
        old = by_number.get(edit["week"])
        if old is None:
            raise ValueError(f"There is no week {edit['week']} in the plan")
        kept = [s for s in old["sessions"] if s["date"] <= today.isoformat()]
        sessions = sorted(
            kept + [{k: s.get(k) for k in SESSION_FIELDS} for s in edit["sessions"]],
            key=lambda s: s["date"],
        )
        weeks.append(
            _with_totals(
                {
                    **old,
                    "flag": edit.get("flag", old["flag"]),
                    "key_session": edit.get("key_session", old["key_session"]),
                    "sessions": sessions,
                }
            )
        )
    return weeks


def _summary(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    def runs(week):
        return {(s["date"], s["type"]): _km(s) for s in week["sessions"] if _is_run(s)}

    old, new = runs(before), runs(after)
    return {
        "week": after["week"],
        "start": after.get("start"),
        "target_km": [before["target_km"], after["target_km"]],
        "long_run_km": [before["long_run_km"], after["long_run_km"]],
        "flag": after["flag"],
        "runs": [
            {"date": day, "type": kind, "km": [old.get((day, kind)), new.get((day, kind))]}
            for day, kind in sorted(old.keys() | new.keys())
            if old.get((day, kind)) != new.get((day, kind))
        ],
    }


def propose(
    conn: sqlite3.Connection,
    weeks: list[dict[str, Any]],
    reason: str,
    source: str,
    today: date,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Validate replacement weeks and log them as a proposed (or rejected) change."""
    current = training.all_weeks(conn)
    by_number = {w["week"]: w for w in current}
    violations = new_violations(current, weeks, context(conn, today))
    pairs = [(by_number[w["week"]], w) for w in weeks if w["week"] in by_number]
    status = "rejected" if violations else "proposed"
    diff = {
        "weeks": [{"before": _snapshot(b), "after": _snapshot(a)} for b, a in pairs],
        "notes": notes or [],
        "violations": violations,
    }
    with conn:
        change_id = conn.execute(
            "INSERT INTO plan_changes (created_at, reason, source, diff, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                reason,
                source,
                json.dumps(diff),
                status,
            ),
        ).lastrowid
    return {
        "change_id": change_id,
        "status": status,
        "violations": violations,
        "notes": notes or [],
        "changes": [_summary(b, a) for b, a in pairs],
    }


def apply(conn: sqlite3.Connection, change_id: int, today: date) -> dict[str, Any]:
    """Write a proposed change to the plan, after checking it is current and still valid."""
    row = conn.execute("SELECT * FROM plan_changes WHERE id = ?", (change_id,)).fetchone()
    if row is None:
        raise ValueError(f"There is no plan change {change_id}")
    if row["status"] != "proposed":
        raise ValueError(f"Plan change {change_id} is {row['status']}, not proposed")
    diff = json.loads(row["diff"])
    current = training.all_weeks(conn)
    by_number = {w["week"]: w for w in current}
    before = [by_number.get(c["before"]["week"]) for c in diff["weeks"]]
    if any(
        not w or _snapshot(w) != c["before"] for w, c in zip(before, diff["weeks"], strict=True)
    ):
        raise ValueError("The plan changed since this was proposed; propose it again")
    weeks = [{**by_number[c["after"]["week"]], **c["after"]} for c in diff["weeks"]]
    violations = new_violations(current, weeks, context(conn, today))
    if violations:
        raise ValueError("No longer valid: " + "; ".join(violations))

    with conn:
        for week in weeks:
            week_id = conn.execute(
                "SELECT id FROM plan_weeks WHERE number = ?", (week["week"],)
            ).fetchone()["id"]
            conn.execute(
                "UPDATE plan_weeks SET flag = ?, target_km = ?, long_run_km = ?, key_session = ? "
                "WHERE id = ?",
                (
                    week["flag"],
                    week["target_km"],
                    week["long_run_km"],
                    week["key_session"],
                    week_id,
                ),
            )
            conn.execute("DELETE FROM sessions WHERE week_id = ?", (week_id,))
            conn.executemany(
                "INSERT INTO sessions (week_id, date, type, title, target_km, target_zone, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(week_id, *(s.get(k) for k in SESSION_FIELDS)) for s in week["sessions"]],
            )
        conn.execute("UPDATE plan_changes SET status = 'applied' WHERE id = ?", (change_id,))
        # Every other open proposal was validated against the plan as it was.
        conn.execute(
            "UPDATE plan_changes SET status = 'rejected' WHERE status = 'proposed' AND id != ?",
            (change_id,),
        )
    return {"change_id": change_id, "status": "applied", "weeks": [w["week"] for w in weeks]}


def changes(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM plan_changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        diff = json.loads(row["diff"])
        entry = {
            "change_id": row["id"],
            "created_at": row["created_at"],
            "reason": row["reason"],
            "source": row["source"],
            "status": row["status"],
        }
        if "weeks" in diff and isinstance(diff["weeks"], list):
            entry["changes"] = [_summary(c["before"], c["after"]) for c in diff["weeks"]]
            entry["notes"] = diff.get("notes", [])
            entry["violations"] = diff.get("violations", [])
        else:
            entry["summary"] = diff  # plan imports
        result.append(entry)
    return result
