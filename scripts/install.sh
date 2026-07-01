#!/usr/bin/env bash
# Legacy path — installer lives at repo root.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/install.sh" "$@"
