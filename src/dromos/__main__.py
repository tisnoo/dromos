"""Command line: `python -m dromos sync|worker|import-plan`. See --help."""

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from dromos import db, garmin, ingest, plan
from dromos.scheduler import add_jobs


def main() -> None:
    parser = argparse.ArgumentParser(prog="dromos")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="pull runs from Garmin once")
    sync.add_argument(
        "--since",
        type=date.fromisoformat,
        help="backfill from this date (default: just before the latest stored run)",
    )
    commands.add_parser("worker", help="sync every hour until stopped")
    import_plan = commands.add_parser("import-plan", help="load a training plan JSON file")
    import_plan.add_argument("path", type=Path)
    import_plan.add_argument(
        "--replace", action="store_true", help="overwrite the plan that is already loaded"
    )
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if args.command == "sync":
        conn = db.connect()
        new_runs = ingest.sync(garmin.connect(), conn, since=args.since)
        total = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        for run in new_runs:
            print(f"new: {run.start_time:%Y-%m-%d %H:%M} {run.distance_km:.2f} km")
        print(f"{len(new_runs)} new runs, {total} in the database")
    elif args.command == "import-plan":
        conn = db.connect()
        data = json.loads(args.path.read_text())
        plan.import_plan(conn, data, reason=f"Imported {args.path.name}", replace=args.replace)
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        print(f"Imported {len(data['weeks'])} weeks, {sessions} sessions")
    else:
        add_jobs(BlockingScheduler()).start()


if __name__ == "__main__":
    main()
