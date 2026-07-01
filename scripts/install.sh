#!/usr/bin/env bash
# SatTrack — full ground-station installer for baseline Linux + RTL-SDR.
#
# Installs everything the stack needs: rtl-sdr, decoders, Kismet (ADS-B + SDR
# handoff), Python venv, and udev/modprobe tweaks. Works on Debian/Ubuntu,
# Fedora/RHEL, Arch, and openSUSE — not Kali-specific.
#
# Usage (from repo root, RTL-SDR plugged in):
#   bash scripts/install.sh
#   source .venv/bin/activate && python run.py doctor && python run.py
#
# Options:
#   --skip-kismet       Skip Kismet (no ADS-B map / no SDR sharing handoff)
#   --skip-satdump      Skip SatDump install/build
#   --skip-gr           Skip gr-satellites build (slow; amateur telemetry only)
#   --skip-meteor       Skip meteor_demod/meteor_decode build
#   --minimal           Same as --skip-gr --skip-meteor
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/pkg_manager.sh
source "$SCRIPT_DIR/lib/pkg_manager.sh"

SKIP_KISMET=0
SKIP_SATDUMP=0
SKIP_GR=0
SKIP_METEOR=0

for arg in "$@"; do
  case "$arg" in
    --skip-kismet)  SKIP_KISMET=1 ;;
    --skip-satdump) SKIP_SATDUMP=1 ;;
    --skip-gr)      SKIP_GR=1 ;;
    --skip-meteor)  SKIP_METEOR=1 ;;
    --minimal)      SKIP_GR=1; SKIP_METEOR=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *) echo "[!] Unknown option: $arg" >&2; exit 1 ;;
  esac
done

log()  { echo "[*] $*"; }
warn() { echo "[!] $*" >&2; }
ok()   { echo "[✓] $*"; }

require_linux() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    warn "This installer targets Linux ground stations. On other OSes: pip install -r requirements.txt"
    exit 1
  fi
}

install_kismet_apt_repo() {
  # Official Kismet repo when the distro package is missing or too old.
  [[ "$PKG_MGR" != "apt" ]] && return 1
  command -v lsb_release >/dev/null 2>&1 || pkg_try_install lsb-release
  local codename
  codename="$(lsb_release -cs 2>/dev/null || true)"
  [[ -z "$codename" ]] && return 1

  log "Adding Kismet official apt repo ($codename) ..."
  local keyring="/usr/share/keyrings/kismet-archive-keyring.gpg"
  if [[ ! -f "$keyring" ]]; then
    sudo mkdir -p /usr/share/keyrings
    wget -qO- https://www.kismetwireless.net/kismet-archive/kismet-archive-keyring.gpg \
      | sudo gpg --dearmor -o "$keyring" 2>/dev/null \
      || wget -qO- https://www.kismetwireless.net/repos/kismet-release.gpg.key \
      | sudo gpg --dearmor -o "$keyring" 2>/dev/null \
      || return 1
  fi
  echo "deb [signed-by=$keyring] https://www.kismetwireless.net/kismet/kismet-release $codename main" \
    | sudo tee /etc/apt/sources.list.d/kismet.list >/dev/null
  pkg_update
}

install_kismet() {
  log "Installing Kismet (ADS-B map + background SDR consumer) ..."
  if ! pkg_try_install "${PKG_KISMET[@]}"; then
    install_kismet_apt_repo && pkg_try_install "${PKG_KISMET[@]}" || true
  fi
  if ! command -v kismet >/dev/null 2>&1; then
    warn "Kismet not installed — ADS-B dashboard and sdr_sharing handoff will not work."
    warn "Install manually: https://www.kismetwireless.net/docs/readme/installing/"
    return 1
  fi

  log "Configuring Kismet for RTL-SDR ADS-B ..."
  sudo install -d -m755 /etc/kismet
  sudo install -m644 "$SCRIPT_DIR/kismet/kismet_site.conf" /etc/kismet/kismet_site.conf

  local user="${SUDO_USER:-${USER:-}}"
  if [[ -n "$user" ]] && id "$user" >/dev/null 2>&1; then
    if getent group kismet >/dev/null 2>&1; then
      sudo usermod -aG kismet "$user" 2>/dev/null || true
      log "Added $user to group 'kismet' (log out/in for group to apply)."
    fi
    for grp in plugdev dialout; do
      getent group "$grp" >/dev/null 2>&1 && sudo usermod -aG "$grp" "$user" 2>/dev/null || true
    done
  fi

  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl daemon-reload 2>/dev/null || true
    sudo systemctl enable kismet 2>/dev/null || true
    if sudo systemctl restart kismet 2>/dev/null; then
      ok "Kismet service running (systemctl status kismet)"
    else
      warn "Kismet service failed to start — plug in the RTL-SDR and run: sudo systemctl start kismet"
    fi
  fi
  ok "Kismet $(kismet --version 2>/dev/null | head -1 || echo installed)"
}

setup_rtlsdr_access() {
  log "Blacklisting DVB-T kernel driver (lets rtl_sdr / Kismet claim the dongle) ..."
  echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf >/dev/null
  if lsmod 2>/dev/null | grep -q dvb_usb_rtl28xxu; then
    warn "DVB driver still loaded — unplug/replug the dongle after install (or reboot)."
  fi

  log "Installing udev rules for RTL-SDR ..."
  sudo tee /etc/udev/rules.d/20-rtlsdr.rules >/dev/null <<'UDEV'
# SatTrack / rtl-sdr — allow non-root access to common RTL2832U dongles
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666", TAG+="uaccess"
UDEV
  sudo udevadm control --reload-rules 2>/dev/null || true
  sudo udevadm trigger 2>/dev/null || true
}

install_noaa_apt() {
  command -v noaa-apt >/dev/null 2>&1 && return 0
  pkg_try_install "${PKG_UNZIP[@]}" "${PKG_SNDFILE[@]}"
  local arch zip=""
  case "$(uname -m)" in
    x86_64)  zip="noaa-apt-1.4.1-x86_64-linux-gnu-nogui.zip" ;;
    aarch64) zip="noaa-apt-1.4.1-aarch64-linux-gnu-nogui.zip" ;;
    armv7l)  zip="noaa-apt-1.4.1-armv7-linux-gnueabihf-nogui.zip" ;;
  esac
  [[ -z "$zip" ]] && { warn "noaa-apt: unsupported arch $(uname -m)"; return 1; }
  log "Installing noaa-apt CLI ($zip) ..."
  ( cd /tmp \
    && wget -q -O "$zip" "https://github.com/martinber/noaa-apt/releases/download/v1.4.1/$zip" \
    && unzip -o "$zip" -d noaa-apt-nogui \
    && sudo install -m755 noaa-apt-nogui/noaa-apt /usr/local/bin/noaa-apt ) \
    && ok "noaa-apt installed" \
    || warn "noaa-apt install failed — https://github.com/martinber/noaa-apt/releases"
}

install_satdump() {
  command -v satdump >/dev/null 2>&1 && { ok "satdump already installed"; return 0; }
  log "Trying distro satdump package ..."
  if pkg_try_install "${PKG_SATDUMP[@]}" && command -v satdump >/dev/null 2>&1; then
    ok "satdump from package manager"
    return 0
  fi
  log "Building SatDump v2 from source (no GUI, RTL-SDR only) ..."
  pkg_try_install \
    "${PKG_BUILD_DEPS[@]}" "${PKG_GIT[@]}" "${PKG_CMAKE[@]}" \
    "${PKG_FFTW[@]}" "${PKG_PNG[@]}" "${PKG_TIFF[@]}" \
    "${PKG_VOLK[@]}" "${PKG_RTLSDR_DEV[@]}" "${PKG_JEMALLOC[@]}" \
    "${PKG_CURL_DEV[@]}" "${PKG_ZSTD[@]}" "${PKG_PKGCONF[@]}"
  SATDUMP_PREFIX=/usr/local SATDUMP_BUILD_DIR=/tmp/SatDump-build bash "$SCRIPT_DIR/build_satdump.sh"
}

install_gr_satellites() {
  if gr_satellites --list_satellites >/dev/null 2>&1; then
    ok "gr_satellites already working"
    return 0
  fi
  log "Building gr-satellites (amateur satellite telemetry) ..."
  pkg_try_install \
    "${PKG_BUILD_DEPS[@]}" "${PKG_GNURADIO[@]}" "${PKG_GNURADIO_DEV[@]}" \
    "${PKG_PYBIND11[@]}" "${PKG_SNDFILE[@]}"
  python3 -m pip install --user construct requests 2>/dev/null \
    || pip3 install --break-system-packages construct requests 2>/dev/null \
    || true
  if ! command -v gnuradio-config-info >/dev/null 2>&1; then
    warn "GNU Radio not found — skipping gr-satellites"
    return 1
  fi
  ( cd /tmp \
    && rm -rf gr-satellites \
    && git clone --depth 1 https://github.com/daniestevez/gr-satellites \
    && cd gr-satellites && mkdir -p build && cd build \
    && cmake -DCMAKE_INSTALL_PREFIX="$(gnuradio-config-info --prefix)" .. \
    && make -j"$(nproc)" \
    && sudo make install \
    && sudo ldconfig ) \
    || { warn "gr-satellites build failed"; return 1; }
  bash "$SCRIPT_DIR/fix_gr_satellites.sh" 2>/dev/null || \
    warn "Run: bash scripts/fix_gr_satellites.sh"
}

patch_config_for_local_kismet() {
  local cfg="$REPO_DIR/config.json"
  [[ -f "$cfg" ]] || cfg="$REPO_DIR/config.example.json"
  [[ -f "$cfg" ]] || return 0
  python3 - "$cfg" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
km = data.setdefault("kismet", {})
if km.get("url", "").startswith("http://10.") or km.get("url") == "http://10.0.10.121:2501":
    km["url"] = "http://127.0.0.1:2501"
    km.setdefault("username", "")
    km.setdefault("password", "")
share = data.setdefault("sdr_sharing", {})
share.setdefault("enabled", True)
share.setdefault("release_command", "systemctl stop kismet")
share.setdefault("reacquire_command", "systemctl start kismet")
share.setdefault("status_command", "systemctl is-active --quiet kismet")
share.setdefault("watchdog", True)
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"  patched {path} for local Kismet (127.0.0.1:2501)")
PY
}

setup_python_venv() {
  log "Creating Python virtualenv at $REPO_DIR/.venv ..."
  if [[ ! -d "$REPO_DIR/.venv" ]]; then
    if [[ ${#PKG_VENV[@]} -gt 0 ]]; then
      pkg_try_install "${PKG_VENV[@]}"
    fi
    python3 -m venv "$REPO_DIR/.venv"
  fi
  # shellcheck disable=SC1091
  source "$REPO_DIR/.venv/bin/activate"
  pip install --upgrade pip -q
  pip install -r "$REPO_DIR/requirements.txt"
  pip install sstv 2>/dev/null || warn "sstv (pysstv) not installed — SSTV wavs archived only"
  ok "Python deps installed in .venv"
}

main() {
  require_linux
  detect_pkg_manager
  log "SatTrack installer — detected package manager: $PKG_MGR"
  log "Repo: $REPO_DIR"

  if [[ ! -f "$REPO_DIR/config.json" ]] && [[ -f "$REPO_DIR/config.example.json" ]]; then
    cp "$REPO_DIR/config.example.json" "$REPO_DIR/config.json"
    log "Created config.json from config.example.json — edit observer location before running."
  fi

  pkg_update
  log "Installing core packages (RTL-SDR, SoX, Python, build tools) ..."
  pkg_install \
    "${PKG_BUILD_DEPS[@]}" "${PKG_RTL_SDR[@]}" "${PKG_SOX[@]}" \
    "${PKG_PYTHON[@]}" "${PKG_PIP[@]}" "${PKG_VENV[@]}" \
    "${PKG_WGET[@]}" "${PKG_UNZIP[@]}" || true

  pkg_try_install "${PKG_DIREWOLF[@]}" || \
    warn "direwolf not installed — APRS/packet decoding unavailable"

  setup_rtlsdr_access

  if [[ "$SKIP_KISMET" -eq 0 ]]; then
    install_kismet || true
    patch_config_for_local_kismet || true
  else
    warn "Skipping Kismet (--skip-kismet)"
  fi

  if [[ "$SKIP_SATDUMP" -eq 0 ]]; then
    install_satdump || warn "SatDump missing — Meteor/APT live capture will fail"
  fi

  install_noaa_apt || true

  if [[ "$SKIP_METEOR" -eq 0 ]]; then
    bash "$SCRIPT_DIR/install_meteor_demod.sh" || warn "meteor_demod build failed (SatDump is the primary Meteor decoder)"
  fi

  if [[ "$SKIP_GR" -eq 0 ]]; then
    install_gr_satellites || true
  fi

  setup_python_venv

  log "Verifying RTL-SDR dongle (rtl_test -t) ..."
  if command -v rtl_test >/dev/null 2>&1; then
    rtl_test -t 2>&1 | head -20 || warn "rtl_test failed — plug in dongle / replug after blacklisting"
  fi

  log "SatTrack readiness check:"
  python "$REPO_DIR/run.py" doctor || true

  cat <<EOF

$(ok "Setup complete.")

Copy-paste to run:
  cd "$REPO_DIR"
  source .venv/bin/activate
  python run.py doctor          # verify tools + dongle
  python run.py passes          # preview upcoming passes
  python run.py                 # start auto-monitoring
  python run.py serve           # web dashboard (optional, port 8082)

Optional — run as a systemd service:
  bash scripts/install_service.sh

Kismet ADS-B map: http://127.0.0.1:2501/  (when dongle is free)
If you were added to group 'kismet' or 'plugdev', log out and back in first.
EOF
}

main "$@"
