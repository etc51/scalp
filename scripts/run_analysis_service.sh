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

ANALYSIS_DAYS="${SCALPER_ANALYSIS_DAYS:-5}"
ANALYSIS_TOP="${SCALPER_ANALYSIS_TOP:-5}"

exec .venv/bin/python3 -m moex_scalper analyze \
  --days "$ANALYSIS_DAYS" \
  --top "$ANALYSIS_TOP" \
  --write-report
