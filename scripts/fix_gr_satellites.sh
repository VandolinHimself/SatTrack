#!/usr/bin/env bash
# Wire up gr-satellites after a source build (symlink + launcher wrapper).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -x /usr/bin/gr_satellites ]]; then
  echo "[!] /usr/bin/gr_satellites missing — run scripts/rebuild_gr_satellites.sh first." >&2
  exit 1
fi

GR_PY=$(python3 -c "import gnuradio, sys; print(sys.executable)" 2>/dev/null || echo "/usr/bin/python3")
PY_VER=$("$GR_PY" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

SP=""
for d in "/usr/lib/python${PY_VER}/site-packages" /usr/lib/python3*/site-packages; do
  [[ -d "$d/satellites" ]] && SP="$d/satellites" && break
done

if [[ -z "$SP" ]]; then
  echo "[!] No satellites package found. Run: bash scripts/rebuild_gr_satellites.sh" >&2
  exit 1
fi

DEST=/usr/lib/python3/dist-packages/satellites
echo "[*] Linking $SP -> $DEST"
[[ -e "$DEST" ]] && [[ ! -L "$DEST" ]] && sudo rm -rf "$DEST"
sudo ln -sfn "$SP" "$DEST"

echo "[*] Installing wrapper -> /usr/local/bin/gr_satellites"
sudo install -m755 "$SCRIPT_DIR/gr_satellites.sh" /usr/local/bin/gr_satellites

echo "[*] Testing..."
if gr_satellites --list_satellites 2>/dev/null | grep -qi funcube; then
  echo "[OK] gr_satellites working."
  exit 0
fi

echo "[!] Still broken (likely Python/GNU Radio ABI mismatch on this Kali build)."
echo "    Run: sudo bash scripts/rebuild_gr_satellites.sh"
echo "    SatTrack keeps running — FUNcube is disabled in config until this passes."
exit 1
