# Dromos

A training plan that adapts to you. Dromos pulls runs from Garmin, compares them against a
marathon training plan, and re-plans the remaining weeks from what was actually run. Users
talk to it through MCP clients (Claude custom connector, Hermes agent).

**Status:** Phase 2 (deploy) live: pushes to `main` deploy automatically. Off-server backups still open. v1 scope is Garmin
running data only; gym/strength data comes later.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # setup
.venv/bin/pytest                                             # tests
.venv/bin/ruff check . && .venv/bin/ruff format .            # lint + format
.venv/bin/python -m dromos sync [--since YYYY-MM-DD]         # pull runs from Garmin into SQLite
.venv/bin/python -m dromos worker                            # hourly sync until stopped
.venv/bin/uvicorn dromos.app:app --port 8000                 # run the app (jobs included)
```

Run tests and ruff before considering a change done.

## Architecture

One service, deployed as a single Docker container on a VPS. Public HTTPS comes from the
server's existing Cloudflare Tunnel (public domain → `127.0.0.1:8000`); no web ports
are open on the server:

- **Ingestion:** hourly job (APScheduler, in-process) pulls new Garmin activities and upserts
  them on `garmin_id`, so re-running is always safe.
- **Storage:** SQLite (WAL mode). Single user, single server.
- **MCP endpoint (`/mcp`):** official MCP Python SDK, streamable HTTP, mounted in FastAPI.
  This is the only way AI clients read or change data.
- **Dashboard (`/dashboard`):** server-rendered HTML (Jinja). Reads straight from the DB; there
  is no AI in the dashboard.
- **Notifications:** Telegram message from the app when a new run is ingested.

Planned stack: Python 3.12+, FastAPI, MCP Python SDK, SQLite, APScheduler, Jinja, Docker
Compose, Cloudflare Tunnel, GitHub Actions → GHCR → SSH deploy.

### Code layout

- `src/dromos/garmin.py`: Garmin login (persisted token store) and normalising raw activities
  into `Run` records. `connect(interactive=False)` uses only the token store (for jobs)
- `src/dromos/db.py`: SQLite connection (WAL), append-only `MIGRATIONS` list tracked with
  `PRAGMA user_version` (never edit an applied migration; add a new one), activity upsert.
  Path from `DROMOS_DB` (default `~/.dromos/dromos.db`)
- `src/dromos/ingest.py`: `sync()` fetches from 7 days before the latest stored run (or
  365 days back on an empty DB) and upserts; `sync_job()` is the scheduled wrapper
- `src/dromos/scheduler.py`: registers jobs on an APScheduler scheduler (hourly Garmin sync,
  nightly DB backup to `DROMOS_BACKUP_DIR`, keeps 14)
- `src/dromos/app.py`: FastAPI app; starts the scheduler in its lifespan, so always run it
  with a single worker. `/docs` and `/openapi.json` are disabled
- `src/dromos/__main__.py`: CLI (`sync`, `worker`)
- `Dockerfile`, `deploy/`: image, server compose file, one-time `setup-server.sh`. `.github/workflows/ci.yml`: test → GHCR → SSH deploy on push to `main`.
  The server address lives only in GitHub secrets and the server's `/opt/dromos/.env`
- `scripts/`: one-off and spike scripts
- `tests/`: pytest; use fixture dicts shaped like real Garmin JSON, never call Garmin in tests

## Re-planning rules

Re-planning is load-aware, not date-shifting. These rules live in deterministic code
(`replan()` + a validator), not in prompts:

- Next week's target is based on actual recent load (last 2–3 weeks), not the original plan
- Missed km are never added on top of later weeks
- Weekly volume rises at most ~10% per week (configurable)
- Long run ≤ ~30% of weekly volume
- Race day and taper weeks are locked
- If the gap can't be closed before the taper, lower the peak; never compress the build
- Every change goes `propose → validate → apply` and is logged in `plan_changes`

AI clients may propose and explain changes; the service validates every change against these
rules before saving. Never add an MCP tool that writes to the plan without going through the
validator.

## Garmin

- Uses the unofficial `garminconnect` library. It can break when Garmin changes its login, so
  keep all Garmin calls inside `garmin.py`.
- First login uses `GARMIN_EMAIL` / `GARMIN_PASSWORD` (+ MFA prompt) and writes tokens to the
  token store (`GARMINTOKENS`, default `~/.dromos/garmin-tokens`). After that only the token
  store is needed; the server never stores the password.
- Distances from Garmin are in metres, durations in seconds, speeds in m/s.

## Rules

- **This repo is public.** Never commit credentials, tokens, `.env`, databases, raw Garmin
  exports (`data/`), personal training data, server IPs or hostnames. Keep `.gitignore` up to
  date when adding new local files.
- No MCP endpoint is exposed without auth (OAuth for claude.ai, bearer token for Hermes).
- Keep it simple: one process, one SQLite file, no extra services unless clearly needed.
