"""One-off: convert the Claude-made marathon plan (HTML artifact) into a plan JSON file.

Usage:
    python scripts/convert_claude_plan.py "Marathon 30 May 2027.html" data/plan.json
    python -m dromos import-plan data/plan.json

The artifact keeps its plan in a script: START, RACE, a week table W and PHASES, plus
functions that build each day's sessions. This mirrors those functions. The output holds
personal training data, so write it to data/ (gitignored).
"""

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

PHASES = [("base", 1, 11), ("build", 12, 26), ("taper", 27, 29)]
FLAGS = {"": None, "cut": "cutback", "peak": "peak", "taper": "taper", "race": "race"}


def parse(html: str) -> tuple[date, date, list]:
    start = re.search(r'const START = new Date\("(\d{4}-\d{2}-\d{2})', html).group(1)
    race = re.search(r'const RACE = new Date\("(\d{4}-\d{2}-\d{2})', html).group(1)
    weeks = re.search(r"const W = (\[.*?\]);\n", html, re.S).group(1)
    return date.fromisoformat(start), date.fromisoformat(race), json.loads(weeks)


def friday(n: int, km: float) -> dict:
    if n <= 11:
        return {
            "type": "easy",
            "title": f"Easy run {km:g} km",
            "target_zone": "Z1",
            "notes": "Conversational. This is base, resist going faster. "
            "Under 89% LTHR the whole way",
        }
    if n >= 27:
        q = max(3, round(km * 0.5))
        return {
            "type": "marathon_pace",
            "title": f"Quality {km:g} km",
            "target_zone": "Z2",
            "notes": f"{q} km at marathon pace (90-94% LTHR) inside an easy run. Taper cuts "
            "volume, NOT intensity. Dropping pace work here erases the taper's benefit "
            "(Bosquet 2007)",
        }
    tempo = max(4, round(km * 0.45))
    return {
        "type": "tempo",
        "title": f"Tempo {km:g} km",
        "target_zone": "Z2",
        "notes": f"2 km easy, {tempo} km at tempo pace (95-99% LTHR, a few words only), rest "
        "easy. This is the moderate-intensity work that makes your distribution pyramidal "
        "rather than polarized",
    }


def long_run(n: int, km: float) -> dict:
    if n == 29:
        return {
            "type": "race",
            "title": "RACE 42.2 km",
            "target_zone": "Z2",
            "notes": "Race day. 42.2 km. Wake time, breakfast and fuel plan you have rehearsed "
            "since week 1",
        }
    cap = (
        " Hard cap 3 h on feet: if you hit it before the distance, stop there. "
        "Benefit plateaus, damage does not."
    )
    spike = (
        " Check the 110% rule before you start: this must not exceed 110% of your longest "
        "run in the last 30 days."
    )
    if 13 <= n <= 26 and km >= 19:
        notes = (
            "Last 25% at marathon pace. Fuel at full race rate, 60-90 g carbs/hr, from week 15."
            + cap
            + spike
        )
    elif km >= 14:
        notes = "All easy. Carry water and start practising fuel, build the rate gradually."
        notes += cap + spike
    else:
        notes = "All easy. Same wake time as race morning." + spike
    zone = "Z1→Z2" if (13 <= n <= 26 and km >= 19) or n >= 27 else "Z1"
    return {"type": "long", "title": f"Long run {km:g} km", "target_zone": zone, "notes": notes}


def gym(n: int, which: str) -> dict | None:
    if n == 29:
        if which == "mon":
            notes = "No lifting. Walk, stretch, stay off your feet"
            return {"type": "mobility", "title": "Mobility only", "notes": notes}
        return None
    if n >= 27:
        if which == "mon":
            notes = "2x5 at 60-70%. Movement, not stimulus"
            return {"type": "gym", "title": "Gym, full body", "notes": notes}
        return None
    sets = "2 sets" if n >= 21 else "3 sets"
    if which == "mon":
        notes = f"Squat {sets} x5 · Bench {sets} x5 @ 75-80% · Rows · Calf raise"
        return {"type": "gym", "title": "Gym, squat day", "notes": notes}
    lift = "RDL" if n >= 12 else "Deadlift"
    notes = f"{lift} {sets} x5 · OHP {sets} x6 · Split squat · Face pull"
    return {"type": "gym", "title": "Gym, pull day", "notes": notes}


def easy(km: float, notes: str) -> dict:
    return {"type": "easy", "title": f"Easy run {km:g} km", "target_zone": "Z1", "notes": notes}


MON_NOTE = "After the lift. It will feel slow. Pace is irrelevant, the zone is the point"
WED_NOTE = "After the lift. Still Z1, no matter how slow that is"


def convert(html: str) -> dict:
    start, race, table = parse(html)
    weeks = []
    for n, long_km, total_km, (mon_km, wed_km, fri_km), flag in table:
        week_start = start + timedelta(weeks=n - 1)
        phase = next(name for name, lo, hi in PHASES if lo <= n <= hi)
        days = {
            0: [gym(n, "mon"), easy(mon_km, MON_NOTE)],
            2: [gym(n, "wed"), easy(wed_km, WED_NOTE)],
            4: [friday(n, fri_km)],
            6: [long_run(n, long_km)],
        }
        sessions = []
        for offset, items in days.items():
            for item in filter(None, items):
                km = re.search(r"([\d.]+) km", item["title"])
                sessions.append(
                    {
                        "date": (week_start + timedelta(days=offset)).isoformat(),
                        "target_km": float(km.group(1)) if km else None,
                        "target_zone": None,
                        **item,
                    }
                )
        weeks.append(
            {
                "number": n,
                "week_start": week_start.isoformat(),
                "phase": phase,
                "flag": FLAGS[flag],
                "target_km": total_km,
                "long_run_km": long_km,
                "key_session": days[4][0]["title"] if n < 29 else "RACE 42.2 km",
                "locked": phase == "taper",
                "sessions": sessions,
            }
        )
    race_sessions = [s for w in weeks for s in w["sessions"] if s["type"] == "race"]
    assert [s["date"] for s in race_sessions] == [race.isoformat()], "race date mismatch"
    return {"weeks": weeks}


if __name__ == "__main__":
    source, dest = Path(sys.argv[1]), Path(sys.argv[2])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(convert(source.read_text()), indent=2, ensure_ascii=False))
    print(f"Wrote {dest}")
