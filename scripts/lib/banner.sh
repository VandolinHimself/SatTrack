#!/usr/bin/env bash
# SatTrack install banner — flashy intro text (ANSI colors when supported).

print_sattrack_banner() {
  local reset bold cyan magenta yellow blue dim
  if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
    reset="$(tput sgr0 2>/dev/null || echo '')"
    bold="$(tput bold 2>/dev/null || echo '')"
    cyan="$(tput setaf 6 2>/dev/null || echo '')"
    magenta="$(tput setaf 5 2>/dev/null || echo '')"
    yellow="$(tput setaf 3 2>/dev/null || echo '')"
    blue="$(tput setaf 4 2>/dev/null || echo '')"
    dim="$(tput dim 2>/dev/null || echo '')"
  else
    reset=; bold=; cyan=; magenta=; yellow=; blue=; dim=
  fi

  if command -v figlet >/dev/null 2>&1; then
    echo
    figlet -f slant "SatTrack" 2>/dev/null | sed "s/^/${cyan}/; s/$/${reset}/" || true
  else
    cat <<BANNER

${cyan}${bold}    ____             __            ______             __
   / __/___  ____  / /____  _____/_  __/___  __  __/ /____  _____
  / _/ / __ \\/ __ \\/ __/ _ \\/ ___// / / __ \\/ / / / __/ _ \\/ ___/
 /___/ / /_/ / /_/ / /_/  __/ /   / / / /_/ / /_/ / /_/  __/ /
/____/ \\____/ .___/\\__/\\___/_/   /_/  \\____/\\__,_/\\__/\\___/_/
           /_/${reset}
BANNER
  fi

  cat <<TAGLINE

${magenta}${bold}  ╔══════════════════════════════════════════════════════════════╗
  ║${reset}  ${yellow}★${reset}  ${bold}AUTOMATED SATELLITE GROUND STATION${reset}  ${yellow}★${reset}                        ${magenta}${bold}║
  ║${reset}     Kismet ADS-B  ${dim}+${reset}  SatDump live decode  ${dim}+${reset}  RTL-SDR scheduling   ${magenta}${bold}║
  ╚══════════════════════════════════════════════════════════════╝${reset}

${blue}  ┌─${reset} Point antenna at sky. Plug in dongle. Answer a few questions. Done.
${blue}  └─${reset} ${dim}No manual config.json editing required.${reset}

TAGLINE
  echo
}
