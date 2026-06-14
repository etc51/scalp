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

mkdir -p runtime

OUTPUT_FILE="$(mktemp)"
cleanup() {
  rm -f "$OUTPUT_FILE"
}
trap cleanup EXIT

.venv/bin/python3 -m moex_scalper watchdog --write-report >"$OUTPUT_FILE"
cat "$OUTPUT_FILE"

RESTART_REQUIRED="$(python3 - <<'PY' "$OUTPUT_FILE"
from pathlib import Path
import json
import sys
data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
print('true' if data.get('restart_required') else 'false')
PY
)"

if [[ "$RESTART_REQUIRED" == "true" ]]; then
  sudo systemctl restart moex-scalper.service
  sudo systemctl restart moex-scalper-dashboard.service
fi
