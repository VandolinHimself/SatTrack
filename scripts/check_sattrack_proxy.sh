#!/usr/bin/env bash
# Quick checks for exposing SatTrack at https://sattracker.luciuswayne.com
set -euo pipefail

echo "=== SatTrack local (:8082) ==="
if curl -sf --max-time 3 http://127.0.0.1:8082/api/health >/dev/null; then
  echo "  OK  http://127.0.0.1:8082/api/health"
else
  echo "  FAIL  nothing listening on 127.0.0.1:8082 — run: python run.py serve"
  exit 1
fi

echo
echo "=== Cloudflare Tunnel ingress (luciuswayne.com uses cloudflared, not Traefik) ==="
if systemctl is-active --quiet cloudflared 2>/dev/null; then
  if journalctl -u cloudflared -n 80 --no-pager 2>/dev/null | grep -q 'sattracker.luciuswayne.com'; then
    echo "  OK  tunnel config mentions sattracker.luciuswayne.com"
    journalctl -u cloudflared -n 80 --no-pager 2>/dev/null \
      | grep 'Updated to new configuration' | tail -1 \
      | sed 's/.*config=/  last config: /'
  else
    echo "  FAIL  sattracker.luciuswayne.com not in cloudflared ingress"
    echo "        Add Public Hostname in Cloudflare Zero Trust:"
    echo "          Networks → Tunnels → <your tunnel> → Public Hostname"
    echo "          Hostname : sattracker.luciuswayne.com"
    echo "          Service  : HTTP → http://127.0.0.1:8082"
  fi
else
  echo "  WARN  cloudflared not running on this host"
fi

echo
echo "=== DNS (run from a machine using public DNS) ==="
if command -v dig >/dev/null 2>&1; then
  dig +short sattracker.luciuswayne.com @1.1.1.1 || true
elif command -v nslookup >/dev/null 2>&1; then
  nslookup sattracker.luciuswayne.com 1.1.1.1 2>/dev/null | tail -n +5 || true
else
  echo "  (install dig/nslookup to test DNS here)"
fi
echo "  Expected: CNAME to *.cfargotunnel.com after adding the public hostname"

echo
echo "=== Traefik note ==="
if [[ -f /etc/traefik/traefik.yml ]]; then
  if command -v traefik >/dev/null 2>&1 && systemctl is-active --quiet traefik 2>/dev/null; then
    grep -q sattracker.luciuswayne.com /etc/traefik/traefik.yml \
      && echo "  OK  traefik.yml has sattrack route" \
      || echo "  WARN  traefik running but no sattrack route — see scripts/reverse-proxy/traefik-sattrack.yml"
  else
    echo "  INFO  /etc/traefik/traefik.yml exists but Traefik is not running."
    echo "        luciuswayne.com is fronted by Cloudflare Tunnel instead."
  fi
fi
