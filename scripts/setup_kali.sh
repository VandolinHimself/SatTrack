#!/usr/bin/env bash
# SatTrack — legacy entry point (was setup_kali.sh).
# Delegates to install.sh at the repo root.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/install.sh" "$@"
