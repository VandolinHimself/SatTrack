#!/usr/bin/env bash
# Build SatDump v2 CLI (no GUI) from source — recommended long-term fix for Kali apt v1.2.3.
set -euo pipefail

PREFIX="${SATDUMP_PREFIX:-/usr/local}"
BUILD_DIR="${SATDUMP_BUILD_DIR:-/tmp/SatDump-build}"

echo "[*] Installing build deps ..."
sudo apt update
sudo apt install -y git build-essential cmake g++ pkgconf \
  libfftw3-dev libpng-dev libtiff-dev libjemalloc-dev libcurl4-openssl-dev \
  libvolk-dev libzstd-dev librtlsdr-dev 2>/dev/null || true

echo "[*] Cloning SatDump ..."
rm -rf "$BUILD_DIR"
git clone --depth 1 --branch v2.0.0 https://github.com/SatDump/SatDump.git "$BUILD_DIR" 2>/dev/null || \
  git clone --depth 1 https://github.com/SatDump/SatDump.git "$BUILD_DIR"

cd "$BUILD_DIR"
mkdir -p build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DBUILD_GUI=OFF \
  -DPLUGIN_HACKRF_SDR_SUPPORT=OFF \
  -DPLUGIN_BLADERF_SDR_SUPPORT=OFF \
  -DPLUGIN_USRP_SDR_SUPPORT=OFF \
  -DPLUGIN_LIMESDR_SDR_SUPPORT=OFF \
  -DPLUGIN_PLUTOSDR_SDR_SUPPORT=OFF \
  ..
make -j"$(nproc)"
sudo make install
sudo ldconfig

echo "[✓] Installed: $(command -v satdump) — $(satdump 2>&1 | head -3 || true)"
