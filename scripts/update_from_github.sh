#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/tbank-latency-check}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-moex-scalper.service}"

cd "$PROJECT_DIR"

if [[ ! -d .git ]]; then
  echo "Project is not a git repository: $PROJECT_DIR" >&2
  exit 1
fi

CURRENT_REV="$(git rev-parse HEAD)"

git fetch --prune origin
REMOTE_REV="$(git rev-parse "origin/$BRANCH")"

if [[ "$CURRENT_REV" == "$REMOTE_REV" ]]; then
  echo "Already up to date at $CURRENT_REV"
  exit 0
fi

git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
python3 -m compileall src >/dev/null

bash scripts/install_server_services.sh "$PROJECT_DIR" "$(id -un)" "$(id -gn)" >/dev/null
sudo systemctl restart "$SERVICE_NAME"
if sudo systemctl list-unit-files moex-scalper-dashboard.service >/dev/null 2>&1; then
  sudo systemctl restart moex-scalper-dashboard.service
fi
if sudo systemctl list-unit-files moex-scalper-watchdog.service >/dev/null 2>&1; then
  sudo systemctl start moex-scalper-watchdog.service || true
fi
if sudo systemctl list-unit-files moex-scalper-preopen.service >/dev/null 2>&1; then
  sudo systemctl start moex-scalper-preopen.service || true
fi
if sudo systemctl list-unit-files moex-scalper-summary.service >/dev/null 2>&1; then
  sudo systemctl start moex-scalper-summary.service || true
fi

NEW_REV="$(git rev-parse HEAD)"
echo "Updated $CURRENT_REV -> $NEW_REV and restarted $SERVICE_NAME"
