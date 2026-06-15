#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common_service_env.sh
source "$SCRIPT_DIR/common_service_env.sh"

prepare_moex_scalper_service

mkdir -p runtime reports

# Let the Python app load .env plus tracked GitHub profiles itself so that
# repo-controlled paper profiles can safely override local machine settings.
exec .venv/bin/python3 -m moex_scalper run
