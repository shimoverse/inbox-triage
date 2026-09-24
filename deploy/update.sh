#!/usr/bin/env bash
# Update a hosted install to the latest main: sudo bash deploy/update.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run as root (sudo)." >&2; exit 1; }
cd /opt/inbox-triage
git pull --ff-only
# Earlier templates defaulted to a fixed beta end date; drop exactly that default (a date set on purpose stays).
[ -f /etc/inbox-triage/env ] && sed -i '/^INBOX_TRIAGE_BETA_ENDS=2026-10-31$/d' /etc/inbox-triage/env
UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --locked --no-dev
install -m 0644 deploy/inbox-triage.service /etc/systemd/system/inbox-triage.service
systemctl daemon-reload
systemctl restart inbox-triage
sleep 2
curl -fsS http://127.0.0.1:8765/healthz && echo " <- healthy"
