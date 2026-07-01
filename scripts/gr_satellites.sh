#!/usr/bin/env bash
# gr_satellites wrapper for Debian/Kali.
#
# Source-built gr-satellites installs its Python module under
# /usr/lib/python3*/site-packages/, which system Python does not read.
# Loading satellites_python before gnuradio.gr also triggers:
#   ImportError: generic_type: type "ax100_decode" referenced unknown base type "gr::block"
#
# This script fixes both: PYTHONPATH + preload gnuradio.gr, then run the real CLI.
set -euo pipefail

GR_BIN="${GR_SATELLITES_BIN:-/usr/bin/gr_satellites}"
if [[ ! -x "$GR_BIN" ]]; then
  echo "gr_satellites: not found at $GR_BIN (build gr-satellites from source first)" >&2
  exit 127
fi

# Ensure cmake-installed module is importable.
SP=""
for d in /usr/lib/python3*/site-packages; do
  if [[ -d "$d/satellites" ]]; then
    SP="$d"
    break
  fi
done
if [[ -n "$SP" ]]; then
  export PYTHONPATH="${SP}${PYTHONPATH:+:$PYTHONPATH}"
fi

export GR_SATELLITES_BIN="$GR_BIN"
exec python3 - "$@" <<'PY'
import os
import runpy
import sys

# Register GNU Radio C++ block types before loading satellites_python (.so).
import gnuradio.gr  # noqa: F401
from gnuradio import blocks  # noqa: F401

sys.argv = ["gr_satellites"] + sys.argv[1:]
runpy.run_path(os.environ["GR_SATELLITES_BIN"], run_name="__main__")
PY
