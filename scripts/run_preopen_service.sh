#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common_service_env.sh
source "$SCRIPT_DIR/common_service_env.sh"

prepare_moex_scalper_service

mkdir -p runtime

STATUS=0

.venv/bin/python3 -m moex_scalper doctor --mode paper --write-report || STATUS=$?
.venv/bin/python3 -m moex_scalper watchdog --write-report || STATUS=$?
.venv/bin/python3 -m moex_scalper summarize --write-report || STATUS=$?

exit "$STATUS"
