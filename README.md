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

## Dashboard

`<DROMOS_BASE_URL>/dashboard` shows the week, weekly km against the plan, sleep, HRV, recent
runs and the next 7 days. Only one Google account can sign in. To set it up:

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), configure the
   OAuth consent screen (External, add yourself as a test user) and create an OAuth client
   ID of type *Web application* with the redirect URI
   `<DROMOS_BASE_URL>/dashboard/auth/callback`.
2. Run `scripts/set-dashboard-env.sh <ssh-host>`. It asks for the client ID, secret and
   your Google address, generates `DROMOS_SESSION_SECRET`, writes them to
   `/opt/dromos/.env` and restarts the app. Re-running keeps the session secret (pass
   `--rotate-session` to sign out everywhere).

Until all four are set the dashboard stays closed.

## Feedback from Hermes

Dromos can push to the [Hermes agent](https://hermes-agent.nousresearch.com/)'s webhook
platform, and Hermes turns each event into a message (e.g. on Telegram):

- **New run** (route `dromos-run`): within the hour after a run syncs, with the run, what was
  planned that day, the week so far and last night's sleep. Only runs from today and
  yesterday are sent, so a backfill stays quiet.
- **Daily brief** (route `dromos-daily`): at 06:00 in `DROMOS_TIMEZONE`, after a fresh sync,
  with today's session, the 7 days ahead, yesterday's runs, the week so far, last night's sleep
  and HRV, and heart-rate targets.

Each payload carries the data itself (webhook runs in Hermes don't get MCP tools by
default), and stays under the 4000 characters Hermes passes on as `{__raw__}`. Requests are
signed with Hermes' generic V2 HMAC scheme.

1. In Hermes, enable the webhook platform and add the two routes to `~/.hermes/config.yaml`,
   with one shared secret (`openssl rand -hex 32`):

   ```yaml
   platforms:
     webhook:
       enabled: true
       extra:
         port: 8644
         routes:
           dromos-run:
             events: ["run_ingested"]
             secret: "<secret>"
             signature: generic
             deliver: telegram
             mirror_to_session: true  # so you can reply to it in the chat
             toolsets: []             # the payload has the data; no tools needed
             prompt: |
               You're my marathon coach. A run just synced from my Garmin. Give short
               feedback: did it match the planned session (distance, zone, HR targets)?
               Easy and long runs should stay easy. Mention sleep if it explains the run.
               {__raw__}
           dromos-daily:
             events: ["daily_brief"]
             secret: "<secret>"
             signature: generic
             deliver: telegram
             mirror_to_session: true
             toolsets: []
             prompt: |
               You're my marathon coach. Send my morning brief: what to do today (session,
               distance, HR target), adjusted for last night's sleep and HRV against my
               baseline. Say it plainly if I should ease off. Keep it short.
               {__raw__}
   ```

   Hermes on the same server: the compose file maps `host.docker.internal` to the host, and
   the firewall must let Docker networks reach the port:
   `ufw allow from 172.16.0.0/12 to any port 8644 proto tcp`.
2. Set `DROMOS_HERMES_WEBHOOK_URL` (the base URL of Hermes' webhooks as seen from the
   Dromos container, e.g. `http://host.docker.internal:8644/webhooks`), `DROMOS_HERMES_WEBHOOK_SECRET`
   and `DROMOS_TIMEZONE` (e.g. `Europe/Amsterdam`) in `/opt/dromos/.env`, and restart.
3. Check it: `docker compose exec app python -m dromos brief` sends today's brief now
   (`--print` shows the payload without sending).

With `mirror_to_session`, replying to a message in Telegram continues from it. For Hermes
to use Dromos in chat, add it under `mcp_servers` in Hermes' `config.yaml` with
`url: <DROMOS_BASE_URL>/mcp` and `headers: {Authorization: "Bearer ${DROMOS_API_TOKEN}"}`.

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
