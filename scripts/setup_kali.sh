#!/usr/bin/env bash
# SatTrack — one-shot setup for Kali/Debian ground-station hosts.
# Installs SDR + decoder tooling and the Python deps, then runs a self-check.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[*] Installing system packages (rtl-sdr, sox, direwolf, satdump if available)..."
sudo apt update
sudo apt install -y rtl-sdr sox python3-pip python3-venv
# direwolf provides `atest` for AX.25/APRS packet decoding (ISS, APRS).
sudo apt install -y direwolf 2>/dev/null || \
  echo "[!] 'direwolf' not installed — APRS/packet decoding will be unavailable."

# gr-satellites: amateur telemetry (FUNcube, 280+ sats). NOT on apt/PyPI/conda-forge.
# Build from source, then run scripts/fix_gr_satellites.sh to wire PYTHONPATH + wrapper.
if ! gr_satellites --list_satellites >/dev/null 2>&1; then
  if [[ ! -x /usr/bin/gr_satellites ]]; then
    echo "[*] Building gr-satellites from source..."
    sudo apt install -y gnuradio gnuradio-dev cmake build-essential git pybind11-dev libsndfile1-dev 2>/dev/null || true
    pip install --break-system-packages construct requests 2>/dev/null || true
    if command -v gnuradio-config-info >/dev/null 2>&1; then
      ( cd /tmp \
        && rm -rf gr-satellites \
        && git clone --depth 1 https://github.com/daniestevez/gr-satellites \
        && cd gr-satellites && mkdir -p build && cd build \
        && cmake -DCMAKE_INSTALL_PREFIX="$(gnuradio-config-info --prefix)" .. \
        && make -j"$(nproc)" \
        && sudo make install \
        && sudo ldconfig ) \
        || echo "[!] gr-satellites build failed."
    fi
  fi
  bash "$SCRIPT_DIR/fix_gr_satellites.sh" 2>/dev/null || \
    echo "[!] Run: bash scripts/fix_gr_satellites.sh after building gr-satellites."
fi

# satdump isn't always in apt; try it, but don't fail the whole script.
sudo apt install -y satdump 2>/dev/null || \
  echo "[!] 'satdump' not in apt — install from https://github.com/SatDump/SatDump."

# noaa-apt: APT *audio* wav decoder (used by the rtl_fm backend + `run.py decode`).
# Not in apt repos. The GUI .deb depends on an old GTK lib that modern Kali
# dropped, so install the CLI-only ("nogui") prebuilt binary instead.
if ! command -v noaa-apt >/dev/null 2>&1; then
  sudo apt install -y unzip libpng16-16 libsndfile1 2>/dev/null || true
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64)  NOAA_ZIP="noaa-apt-1.4.1-x86_64-linux-gnu-nogui.zip" ;;
    aarch64) NOAA_ZIP="noaa-apt-1.4.1-aarch64-linux-gnu-nogui.zip" ;;
    armv7l)  NOAA_ZIP="noaa-apt-1.4.1-armv7-linux-gnueabihf-nogui.zip" ;;
    *)       NOAA_ZIP="" ;;
  esac
  if [ -n "$NOAA_ZIP" ]; then
    echo "[*] Installing noaa-apt (CLI build for $ARCH)..."
    ( cd /tmp \
      && wget -q -O "$NOAA_ZIP" "https://github.com/martinber/noaa-apt/releases/download/v1.4.1/$NOAA_ZIP" \
      && unzip -o "$NOAA_ZIP" -d noaa-apt-nogui \
      && sudo install -m755 noaa-apt-nogui/noaa-apt /usr/local/bin/noaa-apt ) \
      || echo "[!] noaa-apt install failed — see https://github.com/martinber/noaa-apt/releases."
  else
    echo "[!] Unknown arch ($ARCH) — grab noaa-apt from https://github.com/martinber/noaa-apt/releases."
  fi
fi

echo "[*] Blacklisting the DVB-T kernel driver so rtl_fm can claim the dongle..."
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf >/dev/null

echo "[*] Creating Python virtualenv at $REPO_DIR/.venv ..."
python3 -m venv "$REPO_DIR/.venv"
# shellcheck disable=SC1091
source "$REPO_DIR/.venv/bin/activate"
pip install --upgrade pip
pip install -r "$REPO_DIR/requirements.txt"
# Optional: pysstv CLI ('sstv') for decoding ISS SSTV events from a wav.
pip install sstv 2>/dev/null || echo "[!] 'sstv' (pysstv) not installed — SSTV wavs will be archived only."

echo "[*] Verifying RTL-SDR (rtl_test -t)..."
rtl_test -t || echo "[!] rtl_test failed — plug in the dongle / replug after blacklisting."

echo "[*] SatTrack readiness check:"
python "$REPO_DIR/run.py" doctor || true

cat <<EOF

[✓] Setup complete.

Next:
  source "$REPO_DIR/.venv/bin/activate"
  python "$REPO_DIR/run.py" passes      # confirm predictions look right
  python "$REPO_DIR/run.py"             # start auto-monitoring

To run as a service, see scripts/satwatch.service.
EOF
