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

ANALYSIS_DAYS="${SCALPER_ANALYSIS_DAYS:-5}"
ANALYSIS_TOP="${SCALPER_ANALYSIS_TOP:-5}"
OPTIMIZER_DAYS="${SCALPER_OPTIMIZER_DAYS:-5}"
OPTIMIZER_MIN_TRADES="${SCALPER_OPTIMIZER_MIN_TRADES:-5}"
RESEARCH_DAYS="${SCALPER_RESEARCH_DAYS:-5}"
RESEARCH_TOP="${SCALPER_RESEARCH_TOP:-5}"

# Refresh the prerequisite reports inline so tune always sees same-day analysis,
# optimizer, and research outputs even if timers drift or a manual run happens
# before the nightly pipeline finishes.
.venv/bin/python3 -m moex_scalper analyze \
  --days "$ANALYSIS_DAYS" \
  --top "$ANALYSIS_TOP" \
  --write-report

.venv/bin/python3 -m moex_scalper optimize \
  --days "$OPTIMIZER_DAYS" \
  --min-trades "$OPTIMIZER_MIN_TRADES" \
  --write-report

.venv/bin/python3 -m moex_scalper research \
  --days "$RESEARCH_DAYS" \
  --top "$RESEARCH_TOP" \
  --write-report

OUTPUT_FILE="$(mktemp)"
cleanup() {
  rm -f "$OUTPUT_FILE"
}
trap cleanup EXIT

.venv/bin/python3 -m moex_scalper tune --apply --write-report >"$OUTPUT_FILE"
cat "$OUTPUT_FILE"

APPLIED="$(python3 - <<'PY' "$OUTPUT_FILE"
from pathlib import Path
import json
import sys
data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
print('true' if data.get('applied') else 'false')
PY
)"

if [[ "$APPLIED" == "true" ]]; then
  sudo systemctl restart moex-scalper.service
fi
