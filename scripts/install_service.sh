#!/usr/bin/env bash
# Install and enable the SatTrack systemd unit for this repo checkout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_DEST="/etc/systemd/system/satwatch.service"

pick_python() {
  if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    echo "$REPO_DIR/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  return 1
}

PYTHON="$(pick_python)" || {
  echo "[!] No python found. Install deps: bash install.sh"
  exit 1
}

if [[ "$PYTHON" != "$REPO_DIR/.venv/bin/python" ]]; then
  echo "[*] No .venv — using system python: $PYTHON"
fi

if ! (cd "$REPO_DIR" && "$PYTHON" run.py doctor >/dev/null 2>&1); then
  echo "[!] $PYTHON cannot run SatTrack from $REPO_DIR"
  echo "    Try: bash install.sh   (creates .venv + deps)"
  exit 1
fi

TMP="$(mktemp)"
sed -e "s|WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
    -e "s|ExecStart=.*|ExecStart=$PYTHON $REPO_DIR/run.py watch|" \
    "$SCRIPT_DIR/satwatch.service" > "$TMP"

echo "[*] Installing $UNIT_DEST"
echo "    repo : $REPO_DIR"
echo "    python: $PYTHON"
sudo cp "$TMP" "$UNIT_DEST"
rm -f "$TMP"

sudo systemctl daemon-reload
sudo systemctl enable satwatch
echo "[*] Starting satwatch ..."
sudo systemctl restart satwatch
sudo systemctl --no-pager status satwatch
echo
echo "[✓] Follow logs: journalctl -u satwatch -f"
