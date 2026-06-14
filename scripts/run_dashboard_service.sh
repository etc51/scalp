#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/tbank-latency-check}"

cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
  echo "Missing $PROJECT_DIR/.env" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python3 ]]; then
  echo "Missing virtualenv at $PROJECT_DIR/.venv" >&2
  exit 1
fi

while IFS= read -r RAW_LINE || [[ -n "$RAW_LINE" ]]; do
  LINE="${RAW_LINE%$'\r'}"
  if [[ -z "$LINE" || "$LINE" == \#* || "$LINE" != *=* ]]; then
    continue
  fi

  KEY="${LINE%%=*}"
  VALUE="${LINE#*=}"

  if [[ "$VALUE" == \"*\" && "$VALUE" == *\" ]]; then
    VALUE="${VALUE:1:-1}"
  elif [[ "$VALUE" == \'*\' && "$VALUE" == *\' ]]; then
    VALUE="${VALUE:1:-1}"
  fi

  export "$KEY=$VALUE"
done < .env

mkdir -p runtime reports

HOST="${SCALPER_DASHBOARD_HOST:-0.0.0.0}"
PORT="${SCALPER_DASHBOARD_PORT:-8080}"

exec .venv/bin/python3 -m moex_scalper dashboard --host "$HOST" --port "$PORT"
