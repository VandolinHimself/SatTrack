#!/usr/bin/env bash
# SatTrack — legacy entry point (was setup_kali.sh).
# Delegates to scripts/install.sh — works on any supported Linux, not just Kali.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" "$@"
