#!/usr/bin/env bash
# Install systemd unit for `python run.py serve` (dashboard on :8082).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_DEST="/etc/systemd/system/sattrack-serve.service"

pick_python() {
  if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    echo "$REPO_DIR/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  return 1
}

PYTHON="$(pick_python)" || {
  echo "[!] No python found in $REPO_DIR"
  exit 1
}

TMP="$(mktemp)"
sed -e "s|WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
    -e "s|ExecStart=.*|ExecStart=$PYTHON $REPO_DIR/run.py serve --host 0.0.0.0 --port 8082|" \
    "$SCRIPT_DIR/sattrack-serve.service" > "$TMP"

echo "[*] Installing $UNIT_DEST"
echo "    repo   : $REPO_DIR"
echo "    python : $PYTHON"
sudo cp "$TMP" "$UNIT_DEST"
rm -f "$TMP"

sudo systemctl daemon-reload
sudo systemctl enable sattrack-serve
sudo systemctl restart sattrack-serve
sudo systemctl --no-pager status sattrack-serve
echo
echo "[✓] Dashboard: http://$(hostname -I | awk '{print $1}'):8082/"
echo "    Logs: journalctl -u sattrack-serve -f"
