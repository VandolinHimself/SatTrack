#!/usr/bin/env bash
# Copy web dashboard files to the Kali ground-station repo.
# Usage (from repo root on a machine that can ssh to lucius):
#   bash scripts/sync_dashboard_to_kali.sh
#   REMOTE=root@lucius DEST=/home/jellyboy/MCP-Kali/SatTrack bash scripts/sync_dashboard_to_kali.sh

set -euo pipefail
REMOTE="${REMOTE:-root@lucius}"
DEST="${DEST:-/home/jellyboy/MCP-Kali/SatTrack}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Syncing dashboard from $ROOT -> $REMOTE:$DEST"

rsync -av --relative \
  "$ROOT/./sattrack/live.py" \
  "$ROOT/./sattrack/status_store.py" \
  "$ROOT/./sattrack/watcher.py" \
  "$ROOT/./sattrack/telemetry.py" \
  "$ROOT/./sattrack/web/" \
  "$ROOT/./run.py" \
  "$ROOT/./requirements.txt" \
  "$ROOT/./README.md" \
  "$REMOTE:$DEST/"

echo "Done. On lucius run:"
echo "  cd $DEST && pip install -r requirements.txt && python run.py serve"
