#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/opt/tbank-latency-check}"

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Project directory does not exist: $PROJECT_DIR"
  echo "Upload the project first, then rerun this script."
  exit 1
fi

apt update
apt install -y python3.12-venv python3-pip ca-certificates

cd "$PROJECT_DIR"
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
fi

echo
echo "Bootstrap completed."
echo "Edit $PROJECT_DIR/.env, then run:"
echo "source $PROJECT_DIR/.venv/bin/activate"
echo "python3 -m tbank_latency_check --iterations 30 --stream-iterations 5 --write-report"
