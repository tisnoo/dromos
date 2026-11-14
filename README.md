# Dromos

A training plan that adapts to you.

Dromos pulls your runs from Garmin, compares them against your marathon plan, and
re-plans the remaining weeks from what you actually ran. You talk to it through
Claude (custom connector) or any other MCP client.

**Status:** Phase 5, Claude and Hermes can read runs and the plan over MCP, and re-plan
through a rules validator.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Pulling runs from Garmin

```sh
# First time: put your credentials in .env (gitignored). This logs in (asks for an
# MFA code if enabled) and saves tokens to ~/.dromos/garmin-tokens
cp .env.example .env
.venv/bin/python -m dromos sync --since 2026-07-01   # backfill from a date

# After that only the saved tokens are used; you can remove the password from .env
.venv/bin/python -m dromos sync     # fetch new runs (re-running is safe)
.venv/bin/python -m dromos worker   # sync every hour until stopped
```

Runs are stored in `~/.dromos/dromos.db` (set `DROMOS_DB` to change it).
`scripts/garmin_spike.py` prints your last 30 runs and saves the raw JSON to `data/`.

## Loading a training plan

A plan is a JSON file of weeks and sessions; the format is described in
`src/dromos/plan.py`. Keep it in `data/` (gitignored).

```sh
.venv/bin/python scripts/convert_claude_plan.py "Marathon.html" data/plan.json  # from the HTML plan
.venv/bin/python -m dromos import-plan data/plan.json
```

On the server, copy the file to `/opt/dromos/data/` and run
`docker compose exec app python -m dromos import-plan /data/plan.json`.

## Connecting AI clients

The MCP endpoint is `<DROMOS_BASE_URL>/mcp`. Read tools: `get_training_status`,
`get_activities`, `get_sleep`, `get_plan`, `compare_plan_vs_actual`, `get_plan_changes`;
`record_threshold_test` stores a lactate threshold test for heart-rate targets. Plan changes:
`propose_replan` and `propose_plan_change` return a checked proposal; `apply_plan_change`
writes it once you agree.

- **Claude:** Settings → Connectors → Add custom connector, URL `<DROMOS_BASE_URL>/mcp`.
  Claude registers itself and opens the Dromos login page; enter `DROMOS_OWNER_PASSWORD`.
- **Hermes (or any client with a fixed token):** send `Authorization: Bearer <DROMOS_API_TOKEN>`.

Both secrets live in `/opt/dromos/.env` on the server (see `.env.example`).

## Deploying

Pushes to `main` run the tests, build an image to GHCR and deploy it over SSH to
`/opt/dromos` on the server, where Docker Compose runs the app on `127.0.0.1:8000`. The
server's Cloudflare Tunnel provides public HTTPS, so no web ports are open.

One-time setup:

1. Add the hostname to the tunnel's `ingress` in `/etc/cloudflared/config.yml`
   (`service: http://127.0.0.1:8000`), restart `cloudflared`, and add a proxied CNAME
   for it pointing at `<tunnel id>.cfargotunnel.com`.
2. Create a deploy key (`ssh-keygen -t ed25519 -f dromos-deploy -N ''`) and run
   `ssh root@<server> "bash -s -- '$(cat dromos-deploy.pub)'" < deploy/setup-server.sh`.
   This installs Docker, hardens SSH (keys only), enables the firewall (SSH and Tailscale
   only) and unattended upgrades, and creates a `deploy` user and `/opt/dromos`.
3. Copy your local token store to `/opt/dromos/data/garmin-tokens/` (owned by uid 1000).
   App secrets, when there are any, go in `/opt/dromos/.env`.
4. In GitHub, add a `production` environment with the secrets `DEPLOY_HOST`, `DEPLOY_USER`
   (`deploy`), `DEPLOY_SSH_KEY` (the private deploy key) and `DEPLOY_KNOWN_HOSTS`
   (`ssh-keyscan <server>`).
5. After the first build, make the `dromos` package on GHCR public so the server can pull it.

The app keeps 14 nightly database backups in `/opt/dromos/data/backups`.
