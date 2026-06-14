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

if [[ -z "${GRPC_DEFAULT_SSL_ROOTS_FILE_PATH:-}" ]]; then
  CERT_PATH="$(
    .venv/bin/python3 - <<'PY'
from pathlib import Path

try:
    import t_tech.invest as invest
except Exception:
    print("")
    raise SystemExit(0)

cert = Path(invest.__file__).resolve().parent / "certs" / "RussianTrustedRootCA.pem"
print(cert if cert.exists() else "")
PY
  )"
  if [[ -n "$CERT_PATH" ]]; then
    export GRPC_DEFAULT_SSL_ROOTS_FILE_PATH="$CERT_PATH"
  fi
fi

mkdir -p runtime reports

MODE="${SCALPER_MODE:-paper}"
WATCHLIST="${SCALPER_WATCHLIST:-SBER,GAZP,LKOH,VTBR}"
RUN_SECONDS="${SCALPER_RUN_DURATION_SECONDS:-0}"

exec .venv/bin/python3 -m moex_scalper run \
  --mode "$MODE" \
  --watchlist "$WATCHLIST" \
  --run-seconds "$RUN_SECONDS"
