#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common_service_env.sh
source "$SCRIPT_DIR/common_service_env.sh"

prepare_moex_scalper_service

mkdir -p runtime

OUTPUT_FILE="$(mktemp)"
cleanup() {
  rm -f "$OUTPUT_FILE"
}
trap cleanup EXIT

.venv/bin/python3 -m moex_scalper restrict --apply --write-report >"$OUTPUT_FILE"
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
