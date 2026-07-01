#!/usr/bin/env bash
# Disable SatDump plugins that crash apt v1.2.x on Kali before any decode runs.
# Symptom: std::out_of_range in libfirstparty_loader_support.so (exit 134).
# Re-enable: mv *.so.disabled back to *.so in the plugin directory.
set -euo pipefail

SKIP=(
  libfirstparty_loader_support.so
  libremote_sdr_support.so
)

found=0
while IFS= read -r -d '' f; do
  base=$(basename "$f")
  for s in "${SKIP[@]}"; do
    if [[ "$base" == "$s" ]]; then
      if [[ ! -f "${f}.disabled" ]]; then
        echo "[*] Disabling $f"
        mv "$f" "${f}.disabled"
      else
        echo "[*] Already disabled: $f"
      fi
      found=1
    fi
  done
done < <(find /usr/lib -path '*/satdump/plugins/lib*.so' -print0 2>/dev/null)

if [[ "$found" -eq 0 ]]; then
  echo "[!] No SatDump plugin directory found under /usr/lib"
  exit 1
fi

echo "[*] Smoke test ..."
if out=$(satdump 2>&1); then
  echo "$out" | head -5
  echo "[✓] SatDump starts — try: python run.py decode-meteor <capture_folder>"
elif echo "$out" | grep -qiE 'out_of_range|Aborted|terminate called'; then
  echo "$out" | tail -5
  echo "[!] SatDump still crashing on plugins"
  exit 1
else
  echo "$out" | head -8
  echo "[✓] SatDump runs (may show usage) — try decode-meteor"
fi
