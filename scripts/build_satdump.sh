#!/usr/bin/env bash
# Build SatDump v2 CLI (no GUI) from source when the distro package is missing/old.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/pkg_manager.sh
source "$SCRIPT_DIR/lib/pkg_manager.sh"

PREFIX="${SATDUMP_PREFIX:-/usr/local}"
BUILD_DIR="${SATDUMP_BUILD_DIR:-/tmp/SatDump-build}"

detect_pkg_manager
pkg_update
echo "[*] Installing SatDump build deps ..."
pkg_try_install \
  "${PKG_BUILD_DEPS[@]}" "${PKG_GIT[@]}" "${PKG_CMAKE[@]}" \
  "${PKG_FFTW[@]}" "${PKG_PNG[@]}" "${PKG_TIFF[@]}" \
  "${PKG_JEMALLOC[@]}" "${PKG_CURL_DEV[@]}" \
  "${PKG_VOLK[@]}" "${PKG_ZSTD[@]}" "${PKG_RTLSDR_DEV[@]}" \
  "${PKG_PKGCONF[@]}"

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
