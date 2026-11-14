#!/usr/bin/env bash
# Set the dashboard's Google sign-in settings in the server's /opt/dromos/.env and restart
# the app on the image it is already running.
#
#   scripts/set-dashboard-env.sh <ssh-host> [--rotate-session]
#
# Asks for the Google OAuth client ID and secret and your Google address, and generates
# DROMOS_SESSION_SECRET (kept on re-runs unless --rotate-session, which signs you out).
# Values travel over SSH stdin, so they never show up in process lists or shell history.
set -euo pipefail

HOST="${1:?usage: scripts/set-dashboard-env.sh <ssh-host> [--rotate-session]}"
ROTATE=0
[[ "${2:-}" == "--rotate-session" ]] && ROTATE=1

read -rp "Google client ID: " client_id
read -rsp "Google client secret (hidden): " client_secret
echo
read -rp "Your Google address (the only one allowed in): " email

valid='^[A-Za-z0-9._@+-]+$'
[[ "$client_id" == *.apps.googleusercontent.com && "$client_id" =~ $valid ]] \
    || { echo "That doesn't look like a Google client ID (…apps.googleusercontent.com)" >&2; exit 1; }
[[ -n "$client_secret" && "$client_secret" =~ $valid ]] \
    || { echo "The client secret is empty or has unexpected characters" >&2; exit 1; }
[[ "$email" == *@*.* && "$email" =~ $valid ]] \
    || { echo "That doesn't look like an email address" >&2; exit 1; }

# Runs on the server: reads KEY=VALUE lines from stdin into .env, then restarts the app.
REMOTE='
set -euo pipefail
rotate=$1
cd /opt/dromos
umask 077
touch .env
new=$(mktemp .env.new.XXXXXX)
trap "rm -f \"$new\" \"$new.tmp\"" EXIT
cp .env "$new"
while IFS="=" read -r key value; do
    [ -n "$key" ] || continue
    if [ "$key" = DROMOS_SESSION_SECRET ] && [ "$rotate" != 1 ] \
        && grep -q "^DROMOS_SESSION_SECRET=." "$new"; then
        echo "Kept the existing DROMOS_SESSION_SECRET"
        continue
    fi
    grep -v "^$key=" "$new" > "$new.tmp" || true
    mv "$new.tmp" "$new"
    printf "%s=%s\n" "$key" "$value" >> "$new"
    echo "Set $key"
done
# Keep the owner: CI deploys as the deploy user, which must still be able to read .env.
chown --reference=.env "$new" 2>/dev/null || true
cp -p .env .env.bak
mv "$new" .env
echo "Saved .env (previous version in .env.bak)"

image=$(docker inspect --format "{{.Config.Image}}" "$(docker compose ps -q app)")
DROMOS_IMAGE="$image" docker compose up -d
for _ in $(seq 30); do
    status=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/dashboard || true)
    [ "$status" = 303 ] && { echo "Dashboard is up: it redirects to Google sign-in."; exit 0; }
    sleep 1
done
echo "The app restarted, but /dashboard answered $status (expected 303)." >&2
echo "If it is 503, the running image predates the dashboard or a setting is missing." >&2
exit 1
'

printf '%s\n' \
    "GOOGLE_CLIENT_ID=$client_id" \
    "GOOGLE_CLIENT_SECRET=$client_secret" \
    "DROMOS_DASHBOARD_EMAIL=$email" \
    "DROMOS_SESSION_SECRET=$(openssl rand -hex 32)" \
    | ssh "$HOST" "bash -c '$REMOTE' -- $ROTATE"
