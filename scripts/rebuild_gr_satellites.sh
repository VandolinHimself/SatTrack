#!/usr/bin/env bash
# Rebuild gr-satellites against the SAME Python that loads GNU Radio.
# Run when fix_gr_satellites.sh reports gr::block / import errors.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v gnuradio-config-info >/dev/null 2>&1; then
  echo "[!] gnuradio-config-info not found — install gnuradio first." >&2
  exit 1
fi

# Use the interpreter that actually imports gnuradio (avoids py3.13 ext + py3.11 gnuradio mismatch).
GR_PY=$(python3 -c "import gnuradio, sys; print(sys.executable)" 2>/dev/null || true)
if [[ -z "$GR_PY" ]]; then
  echo "[!] python3 cannot import gnuradio — install gnuradio first." >&2
  exit 1
fi

PY_VER=$("$GR_PY" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[*] GNU Radio Python: $GR_PY ($PY_VER)"
echo "[*] GNU Radio version: $(gnuradio-config-info --version)"
echo "[*] Install prefix:    $(gnuradio-config-info --prefix)"

sudo apt install -y gnuradio gnuradio-dev cmake build-essential git pybind11-dev libsndfile1-dev
"$GR_PY" -m pip install --break-system-packages construct requests 2>/dev/null || \
  "$GR_PY" -m pip install construct requests 2>/dev/null || true

BUILD=/tmp/gr-satellites-rebuild
rm -rf "$BUILD"
git clone --depth 1 https://github.com/daniestevez/gr-satellites "$BUILD/src"
mkdir -p "$BUILD/src/build"
cd "$BUILD/src/build"

cmake \
  -DCMAKE_INSTALL_PREFIX="$(gnuradio-config-info --prefix)" \
  -DPython3_EXECUTABLE="$GR_PY" \
  -DPYTHON_EXECUTABLE="$GR_PY" \
  ..

make -j"$(nproc)"
sudo make install
sudo ldconfig

# Remove stale copies that cause ABI mismatches.
sudo rm -rf /usr/lib/python3/dist-packages/satellites
SP=$(ls -d "/usr/lib/python${PY_VER}/site-packages/satellites" 2>/dev/null | head -1 || true)
if [[ -z "$SP" ]]; then
  SP=$(find /usr/lib/python3* -path '*/site-packages/satellites' -type d 2>/dev/null | head -1 || true)
fi

if [[ -n "$SP" ]]; then
  echo "[*] Linking $SP -> /usr/lib/python3/dist-packages/satellites"
  sudo ln -sfn "$SP" /usr/lib/python3/dist-packages/satellites
else
  echo "[!] Could not find installed satellites python package." >&2
  exit 1
fi

sudo install -m755 "$SCRIPT_DIR/gr_satellites.sh" /usr/local/bin/gr_satellites

echo "[*] Testing import (clean env)..."
if env -i HOME="$HOME" USER="${USER:-root}" PATH="/usr/local/bin:/usr/bin:/bin" \
  "$GR_PY" -c "
import gnuradio.gr
from gnuradio import blocks  # noqa: F401 — register block types
import satellites.core
print('IMPORT OK')
"; then
  echo "[OK] gr-satellites rebuilt and importable."
  gr_satellites --list_satellites 2>/dev/null | grep -i funcube || true
  echo ""
  echo "Re-enable AO-73 in config.json:"
  echo '  "decoder": "gr_satellites", "gr_name": "FUNcube-1", "enabled": true'
else
  echo "[!] Import still failing on this Python/GNU Radio combo." >&2
  echo "    FUNcube telemetry stays disabled; everything else still works." >&2
  exit 1
fi
