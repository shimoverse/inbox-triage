#!/usr/bin/env bash
# One-command install of the hosted Inbox Triage server on Debian/Ubuntu.
#   sudo DOMAIN=triage.example.com bash deploy/install.sh
# Installs: uv, the app (in /opt/inbox-triage), a systemd service, and Caddy for automatic HTTPS.
# Re-running is safe; it never overwrites /etc/inbox-triage/env.
set -euo pipefail

: "${DOMAIN:?Set DOMAIN, e.g. sudo DOMAIN=triage.example.com bash deploy/install.sh}"
REPO_URL=${REPO_URL:-https://github.com/shimoverse/inbox-triage.git}
APP_DIR=/opt/inbox-triage
DATA_DIR=/var/lib/inbox-triage
ENV_FILE=/etc/inbox-triage/env

[ "$(id -u)" -eq 0 ] || { echo "Please run as root (sudo)." >&2; exit 1; }

echo "==> Packages"
apt-get update -y
apt-get install -y curl git ca-certificates gnupg debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Caddy (official repository)"
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "==> uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

echo "==> Service user and directories"
id triage >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin triage
install -d -o triage -g triage -m 0700 "$DATA_DIR"
install -d -o root -g triage -m 0750 /etc/inbox-triage

echo "==> Application"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
# Reproducible install from uv.lock into $APP_DIR/.venv (owned by root, read-only to the service).
(cd "$APP_DIR" && UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --locked --no-dev)

if [ ! -f "$ENV_FILE" ]; then
  echo "==> $ENV_FILE (fill in the OAuth client)"
  install -o root -g triage -m 0640 /dev/null "$ENV_FILE"
  cat > "$ENV_FILE" <<ENV
PUBLIC_URL=https://$DOMAIN
# Google OAuth client ("Web application", redirect https://$DOMAIN/oauth/callback). See docs/google-cloud-setup.md
INBOX_TRIAGE_OAUTH_CLIENT_ID=
INBOX_TRIAGE_OAUTH_CLIENT_SECRET=
# Optional notes assistant (DeepSeek V4.1 Flash via OpenRouter), paid by the operator:
OPENROUTER_API_KEY=
# Do NOT set TYPESAFE_API_KEY here: every user connects their own Jev key.
ENV
fi

echo "==> systemd"
install -m 0644 "$APP_DIR/deploy/inbox-triage.service" /etc/systemd/system/inbox-triage.service
systemctl daemon-reload
systemctl enable inbox-triage >/dev/null

echo "==> Caddy (HTTPS for $DOMAIN)"
sed "s/{DOMAIN}/$DOMAIN/" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

if grep -q '^INBOX_TRIAGE_OAUTH_CLIENT_ID=.\+' "$ENV_FILE"; then
  systemctl restart inbox-triage
  echo "Done. Open https://$DOMAIN"
else
  echo
  echo "Almost done: add the Google OAuth client ID and secret to $ENV_FILE, then run:"
  echo "  sudo systemctl restart inbox-triage"
fi
echo "Health check: curl -s https://$DOMAIN/healthz"
