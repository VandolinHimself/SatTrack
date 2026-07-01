#!/usr/bin/env bash
# Build meteor_demod + meteor_decode — METEOR LRPT decode without SatDump.
set -euo pipefail

echo "[*] Installing build deps ..."
sudo apt update
sudo apt install -y build-essential cmake git libvolk-dev libsndfile1-dev fpc libpng-dev

WORKDIR="${METEOR_BUILD_DIR:-/tmp/meteor-lrpt-build}"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

if ! command -v meteor_demod >/dev/null 2>&1; then
  echo "[*] Building meteor_demod ..."
  rm -rf meteor_demod
  git clone --depth 1 https://github.com/dbdexter-dev/meteor_demod.git
  cmake -S meteor_demod -B meteor_demod/build -DCMAKE_BUILD_TYPE=Release
  cmake --build meteor_demod/build -j"$(nproc)"
  sudo install -m755 meteor_demod/build/meteor_demod /usr/local/bin/meteor_demod
else
  echo "[*] meteor_demod already installed: $(command -v meteor_demod)"
fi

if ! command -v meteor_decode >/dev/null 2>&1; then
  echo "[*] Building meteor_decode ..."
  rm -rf meteor_decode
  git clone --depth 1 https://github.com/dbdexter-dev/meteor_decode.git
  cmake -S meteor_decode -B meteor_decode/build -DCMAKE_BUILD_TYPE=Release
  cmake --build meteor_decode/build -j"$(nproc)"
  sudo install -m755 meteor_decode/build/meteor_decode /usr/local/bin/meteor_decode
else
  echo "[*] meteor_decode already installed: $(command -v meteor_decode)"
fi

echo "[✓] meteor_demod:  $(meteor_demod --version 2>&1 | head -1 || true)"
echo "[✓] meteor_decode: $(meteor_decode --version 2>&1 | head -1 || true)"
echo "    Decode: python run.py decode-meteor captures/<METEOR_folder>/"
