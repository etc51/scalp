#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/tbank-latency-check}"
cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
  echo ".env not found in $PROJECT_DIR" >&2
  exit 1
fi

while IFS='=' read -r key value; do
  [[ -z "$key" ]] && continue
  [[ "$key" =~ ^# ]] && continue
  export "$key"="${value:-}"
done < .env

mkdir -p runtime

exec .venv/bin/python3 -m moex_scalper summarize --write-report
