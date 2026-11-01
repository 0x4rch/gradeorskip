#!/usr/bin/env bash
# Expose the local server over HTTPS with a Cloudflare quick tunnel (no account, no certs),
# so the phone gets the live guided camera. Prints the private link and a QR code.
# The link only works while this script runs. Usage: ./share.sh [port]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8790}"
CF=$(command -v cloudflared || echo .tools/cloudflared)
[[ -x "$CF" ]] || { echo "cloudflared not found. Install it: sudo pacman -S cloudflared"; exit 1; }
KEY=$(grep '^GRADEORSKIP_ACCESS_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
[[ -n "$KEY" ]] || { echo "Add GRADEORSKIP_ACCESS_KEY to .env first (it gates tunnel traffic)."; exit 1; }
curl -sf "localhost:$PORT/healthz" >/dev/null || { echo "Start the server first: ./dev.sh $PORT"; exit 1; }

log=$(mktemp)
"$CF" tunnel --no-autoupdate --url "http://localhost:$PORT" >"$log" 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null; rm -f "$log"' EXIT
url=""
for _ in $(seq 1 60); do
  url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" | head -1 || true)
  [[ -n "$url" ]] && break
  sleep 0.5
done
[[ -n "$url" ]] || { echo "Tunnel didn't start:"; cat "$log"; exit 1; }

link="$url/?key=$KEY"
echo
echo "Open on your phone (Ctrl-C here to stop sharing):"
echo "  $link"
echo
.venv/bin/python -c "import qrcode,sys; q=qrcode.QRCode(border=1); q.add_data(sys.argv[1]); q.print_ascii(invert=True)" "$link" 2>/dev/null || true
wait $pid
