#!/usr/bin/env bash
# Wrapper around system satdump (use after scripts/fix_satdump_plugins.sh).
set -euo pipefail
if ! command -v satdump >/dev/null 2>&1; then
  echo "satdump not found" >&2
  exit 127
fi
exec satdump "$@"
