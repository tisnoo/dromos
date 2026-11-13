"""Phase 0 spike: can we log in to Garmin and pull runs?

Usage:
    cp .env.example .env   # fill in GARMIN_EMAIL / GARMIN_PASSWORD for the first login
    python scripts/garmin_spike.py

Prints the last 30 runs and saves the raw JSON to data/garmin_runs.json
(gitignored) so we can see which fields Garmin actually returns.
"""

import json
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from dromos.garmin import connect, fetch_raw_runs, normalise


def main() -> None:
    load_dotenv()
    client = connect()
    end = date.today()
    raw = fetch_raw_runs(client, end - timedelta(days=180), end)[-30:]

    out = Path("data/garmin_runs.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(raw, indent=2, default=str))

    print(f"{'date':<17} {'km':>6} {'pace':>6} {'avgHR':>6}  zones (min, Z1-Z5)")
    for run in map(normalise, raw):
        pace = run.avg_pace_s_per_km
        pace_str = f"{int(pace // 60)}:{int(pace % 60):02d}" if pace else "-"
        zones = " ".join(f"{run.hr_zone_seconds.get(z, 0) / 60:.0f}" for z in range(1, 6))
        print(
            f"{run.start_time:%Y-%m-%d %H:%M} {run.distance_km:>6.2f} {pace_str:>6} "
            f"{run.avg_hr or '-':>6}  {zones}"
        )
    print(f"\n{len(raw)} runs, raw JSON saved to {out}")


if __name__ == "__main__":
    main()
