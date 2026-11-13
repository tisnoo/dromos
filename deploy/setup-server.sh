#!/usr/bin/env bash
# One-time server setup, run as root on a fresh-ish Ubuntu/Debian server:
#   ssh root@server "bash -s -- '$(cat dromos-deploy.pub)'" < deploy/setup-server.sh
# (quote the key inside the remote command: ssh joins arguments with spaces)
# Safe to re-run. Keep a second SSH session open while running it, in case the
# SSH changes lock you out.
set -euo pipefail

DEPLOY_KEY="${1:?usage: setup-server.sh '<deploy public key>'}"
APP_DIR=/opt/dromos
[[ "$DEPLOY_KEY" == ssh-*" "* ]] || { echo "not a full public key: '$DEPLOY_KEY'" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq ufw unattended-upgrades
if ! command -v docker >/dev/null; then
    apt-get install -yq docker.io docker-compose-v2
fi
dpkg-reconfigure -f noninteractive unattended-upgrades

# Deploy user: used by GitHub Actions; can run docker (which is root-equivalent).
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
grep -qxF "$DEPLOY_KEY" /home/deploy/.ssh/authorized_keys 2>/dev/null \
    || echo "$DEPLOY_KEY" >> /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

# App directory; data/ is owned by the container user (uid 1000).
install -d -o deploy -g deploy "$APP_DIR"
install -d -o 1000 -g 1000 -m 700 "$APP_DIR/data"

# SSH: keys only. The 00- prefix wins over cloud-init's drop-in (first value is used).
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd

# Firewall: only SSH from the internet. Web traffic arrives through the Cloudflare
# Tunnel (outbound), so no web ports are opened. Tailscale keeps working: its UDP port
# and anything on the tailnet interface are allowed. Docker-published ports bypass
# ufw, so compose.yaml only publishes on 127.0.0.1.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 41641/udp
ufw allow in on tailscale0
ufw --force enable

echo "Done. Next: copy the Garmin token store to $APP_DIR/data/garmin-tokens (owned by 1000)"
