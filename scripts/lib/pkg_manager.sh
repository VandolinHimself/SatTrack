#!/usr/bin/env bash
# Shared package-manager helpers for SatTrack install scripts.
# Source from other scripts:  source "$(dirname "$0")/lib/pkg_manager.sh"

set -euo pipefail

PKG_MGR=""
PKG_UPDATE=()
PKG_INSTALL=()
PKG_BUILD_DEPS=()
PKG_RTL_SDR=()
PKG_SOX=()
PKG_PYTHON=()
PKG_PIP=()
PKG_VENV=()
PKG_DIREWOLF=()
PKG_KISMET=()
PKG_SATDUMP=()
PKG_UNZIP=()
PKG_WGET=()
PKG_CMAKE=()
PKG_GIT=()
PKG_FFTW=()
PKG_PNG=()
PKG_TIFF=()
PKG_VOLK=()
PKG_RTLSDR_DEV=()
PKG_GNURADIO=()
PKG_GNURADIO_DEV=()
PKG_PYBIND11=()
PKG_SNDFILE=()
PKG_FPC=()
PKG_JEMALLOC=()
PKG_CURL_DEV=()
PKG_ZSTD=()
PKG_PKGCONF=()

detect_pkg_manager() {
  if [[ -n "${PKG_MGR:-}" ]]; then
    return 0
  fi
  if command -v apt-get >/dev/null 2>&1; then
    PKG_MGR=apt
  elif command -v dnf >/dev/null 2>&1; then
    PKG_MGR=dnf
  elif command -v yum >/dev/null 2>&1; then
    PKG_MGR=yum
  elif command -v pacman >/dev/null 2>&1; then
    PKG_MGR=pacman
  elif command -v zypper >/dev/null 2>&1; then
    PKG_MGR=zypper
  else
    echo "[!] Unsupported Linux distro — need apt, dnf, pacman, or zypper." >&2
    return 1
  fi

  case "$PKG_MGR" in
    apt)
      PKG_UPDATE=(sudo apt-get update -qq)
      PKG_INSTALL=(sudo apt-get install -y)
      PKG_BUILD_DEPS=(build-essential cmake g++ pkg-config git wget unzip)
      PKG_RTL_SDR=(rtl-sdr)
      PKG_SOX=(sox libsox-fmt-all)
      PKG_PYTHON=(python3 python3-dev)
      PKG_PIP=(python3-pip)
      PKG_VENV=(python3-venv)
      PKG_DIREWOLF=(direwolf)
      PKG_KISMET=(kismet)
      PKG_SATDUMP=(satdump)
      PKG_UNZIP=(unzip)
      PKG_WGET=(wget ca-certificates)
      PKG_CMAKE=(cmake)
      PKG_GIT=(git)
      PKG_FFTW=(libfftw3-dev)
      PKG_PNG=(libpng-dev)
      PKG_TIFF=(libtiff-dev)
      PKG_VOLK=(libvolk-dev)
      PKG_RTLSDR_DEV=(librtlsdr-dev)
      PKG_GNURADIO=(gnuradio)
      PKG_GNURADIO_DEV=(gnuradio-dev)
      PKG_PYBIND11=(pybind11-dev)
      PKG_SNDFILE=(libsndfile1-dev libsndfile1)
      PKG_FPC=(fp-compiler)
      PKG_JEMALLOC=(libjemalloc-dev)
      PKG_CURL_DEV=(libcurl4-openssl-dev)
      PKG_ZSTD=(libzstd-dev)
      PKG_PKGCONF=(pkg-config)
      ;;
    dnf|yum)
      local mgr="$PKG_MGR"
      PKG_UPDATE=(sudo "$mgr" check-update -q || true)
      PKG_INSTALL=(sudo "$mgr" install -y)
      PKG_BUILD_DEPS=(gcc gcc-c++ make cmake pkgconf-pkg-config git wget unzip)
      PKG_RTL_SDR=(rtl-sdr)
      PKG_SOX=(sox)
      PKG_PYTHON=(python3 python3-devel)
      PKG_PIP=(python3-pip)
      PKG_VENV=()
      PKG_DIREWOLF=(direwolf)
      PKG_KISMET=(kismet)
      PKG_SATDUMP=(satdump)
      PKG_UNZIP=(unzip)
      PKG_WGET=(wget ca-certificates)
      PKG_CMAKE=(cmake)
      PKG_GIT=(git)
      PKG_FFTW=(fftw-devel)
      PKG_PNG=(libpng-devel)
      PKG_TIFF=(libtiff-devel)
      PKG_VOLK=(volk-devel)
      PKG_RTLSDR_DEV=(librtlsdr-devel)
      PKG_GNURADIO=(gnuradio)
      PKG_GNURADIO_DEV=(gnuradio-devel)
      PKG_PYBIND11=(pybind11-devel)
      PKG_SNDFILE=(libsndfile-devel)
      PKG_FPC=(fpc)
      PKG_JEMALLOC=(jemalloc-devel)
      PKG_CURL_DEV=(libcurl-devel)
      PKG_ZSTD=(libzstd-devel)
      PKG_PKGCONF=(pkgconf-pkg-config)
      ;;
    pacman)
      PKG_UPDATE=(sudo pacman -Sy --noconfirm)
      PKG_INSTALL=(sudo pacman -S --needed --noconfirm)
      PKG_BUILD_DEPS=(base-devel cmake git wget unzip)
      PKG_RTL_SDR=(rtl-sdr)
      PKG_SOX=(sox)
      PKG_PYTHON=(python)
      PKG_PIP=(python-pip)
      PKG_VENV=()
      PKG_DIREWOLF=(direwolf)
      PKG_KISMET=(kismet)
      PKG_SATDUMP=(satdump)
      PKG_UNZIP=(unzip)
      PKG_WGET=(wget ca-certificates)
      PKG_CMAKE=(cmake)
      PKG_GIT=(git)
      PKG_FFTW=(fftw)
      PKG_PNG=(libpng)
      PKG_TIFF=(libtiff)
      PKG_VOLK=(volk)
      PKG_RTLSDR_DEV=(librtlsdr)
      PKG_GNURADIO=(gnuradio)
      PKG_GNURADIO_DEV=(gnuradio)
      PKG_PYBIND11=(pybind11)
      PKG_SNDFILE=(libsndfile)
      PKG_FPC=(fpc)
      PKG_JEMALLOC=(jemalloc)
      PKG_CURL_DEV=(curl)
      PKG_ZSTD=(zstd)
      PKG_PKGCONF=(pkgconf)
      ;;
    zypper)
      PKG_UPDATE=(sudo zypper refresh -q)
      PKG_INSTALL=(sudo zypper install -y)
      PKG_BUILD_DEPS=(patterns-devel-base-devel_basis cmake git wget unzip)
      PKG_RTL_SDR=(rtl-sdr)
      PKG_SOX=(sox)
      PKG_PYTHON=(python3 python3-devel)
      PKG_PIP=(python3-pip)
      PKG_VENV=(python3-virtualenv)
      PKG_DIREWOLF=(direwolf)
      PKG_KISMET=(kismet)
      PKG_SATDUMP=()
      PKG_UNZIP=(unzip)
      PKG_WGET=(wget ca-certificates)
      PKG_CMAKE=(cmake)
      PKG_GIT=(git)
      PKG_FFTW=(libfftw3-threads-devel)
      PKG_PNG=(libpng16-devel)
      PKG_TIFF=(libtiff-devel)
      PKG_VOLK=(libvolk-devel)
      PKG_RTLSDR_DEV=(librtlsdr0-devel)
      PKG_GNURADIO=(gnuradio)
      PKG_GNURADIO_DEV=(gnuradio-devel)
      PKG_PYBIND11=(python3-pybind11-devel)
      PKG_SNDFILE=(libsndfile-devel)
      PKG_FPC=(fpc)
      PKG_JEMALLOC=(libjemalloc2)
      PKG_CURL_DEV=(libcurl-devel)
      PKG_ZSTD=(libzstd-devel)
      PKG_PKGCONF=(pkg-config)
      ;;
  esac
}

pkg_update() {
  detect_pkg_manager
  "${PKG_UPDATE[@]}" || true
}

pkg_install() {
  detect_pkg_manager
  local pkgs=("$@")
  [[ ${#pkgs[@]} -eq 0 ]] && return 0
  if ! "${PKG_INSTALL[@]}" "${pkgs[@]}" 2>/dev/null; then
    echo "[!] Some packages unavailable via $PKG_MGR: ${pkgs[*]}" >&2
    return 1
  fi
}

pkg_try_install() {
  pkg_install "$@" 2>/dev/null || true
}
