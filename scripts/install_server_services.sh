#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/opt/tbank-latency-check}"
RUN_USER="${2:-codex}"
RUN_GROUP="${3:-$RUN_USER}"
SYSTEMD_DIR="/etc/systemd/system"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

render_unit() {
  local template_path="$1"
  local output_path="$2"
  sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__RUN_USER__|$RUN_USER|g" \
    -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
    "$template_path" >"$output_path"
}

render_unit "deploy/systemd/moex-scalper.service" "$TMP_DIR/moex-scalper.service"
render_unit "deploy/systemd/moex-scalper-dashboard.service" "$TMP_DIR/moex-scalper-dashboard.service"
render_unit "deploy/systemd/moex-scalper-analyze.service" "$TMP_DIR/moex-scalper-analyze.service"
render_unit "deploy/systemd/moex-scalper-analyze.timer" "$TMP_DIR/moex-scalper-analyze.timer"
render_unit "deploy/systemd/moex-scalper-optimize.service" "$TMP_DIR/moex-scalper-optimize.service"
render_unit "deploy/systemd/moex-scalper-optimize.timer" "$TMP_DIR/moex-scalper-optimize.timer"
render_unit "deploy/systemd/moex-scalper-tune.service" "$TMP_DIR/moex-scalper-tune.service"
render_unit "deploy/systemd/moex-scalper-tune.timer" "$TMP_DIR/moex-scalper-tune.timer"
render_unit "deploy/systemd/moex-scalper-update.service" "$TMP_DIR/moex-scalper-update.service"
render_unit "deploy/systemd/moex-scalper-update.timer" "$TMP_DIR/moex-scalper-update.timer"

sudo cp "$TMP_DIR/moex-scalper.service" "$SYSTEMD_DIR/moex-scalper.service"
sudo cp "$TMP_DIR/moex-scalper-dashboard.service" "$SYSTEMD_DIR/moex-scalper-dashboard.service"
sudo cp "$TMP_DIR/moex-scalper-analyze.service" "$SYSTEMD_DIR/moex-scalper-analyze.service"
sudo cp "$TMP_DIR/moex-scalper-analyze.timer" "$SYSTEMD_DIR/moex-scalper-analyze.timer"
sudo cp "$TMP_DIR/moex-scalper-optimize.service" "$SYSTEMD_DIR/moex-scalper-optimize.service"
sudo cp "$TMP_DIR/moex-scalper-optimize.timer" "$SYSTEMD_DIR/moex-scalper-optimize.timer"
sudo cp "$TMP_DIR/moex-scalper-tune.service" "$SYSTEMD_DIR/moex-scalper-tune.service"
sudo cp "$TMP_DIR/moex-scalper-tune.timer" "$SYSTEMD_DIR/moex-scalper-tune.timer"
sudo cp "$TMP_DIR/moex-scalper-update.service" "$SYSTEMD_DIR/moex-scalper-update.service"
sudo cp "$TMP_DIR/moex-scalper-update.timer" "$SYSTEMD_DIR/moex-scalper-update.timer"

sudo systemctl daemon-reload
sudo systemctl enable moex-scalper.service
sudo systemctl enable moex-scalper-dashboard.service
sudo systemctl enable --now moex-scalper-analyze.timer
sudo systemctl enable --now moex-scalper-optimize.timer
sudo systemctl enable --now moex-scalper-tune.timer
sudo systemctl enable --now moex-scalper-update.timer

echo "Installed:"
echo "- moex-scalper.service"
echo "- moex-scalper-dashboard.service"
echo "- moex-scalper-analyze.service"
echo "- moex-scalper-analyze.timer"
echo "- moex-scalper-optimize.service"
echo "- moex-scalper-optimize.timer"
echo "- moex-scalper-tune.service"
echo "- moex-scalper-tune.timer"
echo "- moex-scalper-update.service"
echo "- moex-scalper-update.timer"
echo
echo "Start bot:"
echo "  sudo systemctl start moex-scalper.service"
echo
echo "Status:"
echo "  sudo systemctl status moex-scalper.service"
echo
echo "Logs:"
echo "  sudo journalctl -u moex-scalper.service -f"
echo "  sudo journalctl -u moex-scalper-dashboard.service -f"
echo "  sudo journalctl -u moex-scalper-analyze.service -f"
echo "  sudo journalctl -u moex-scalper-optimize.service -f"
echo "  sudo journalctl -u moex-scalper-tune.service -f"
