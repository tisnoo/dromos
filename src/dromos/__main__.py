"""Command line: `python -m dromos sync|worker|import-plan|count-run-as`. See --help."""

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
    count_as = commands.add_parser(
        "count-run-as", help="count the run on one day towards another day of the plan"
    )
    count_as.add_argument("run_date", type=date.fromisoformat, help="the run's date in Garmin")
    count_as.add_argument(
        "count_as", type=date.fromisoformat, nargs="?", help="plan day (omit to undo)"
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
    elif args.command == "count-run-as":
        conn = db.connect()
        garmin_id = db.set_count_as_date(conn, args.run_date, args.count_as)
        target = args.count_as or args.run_date
        print(f"Run {garmin_id} from {args.run_date} now counts as {target}")
    else:
        add_jobs(BlockingScheduler()).start()


if __name__ == "__main__":
    main()
