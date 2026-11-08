#!/bin/zsh
# Temporary public HTTPS address for testing TradingView webhooks from the Mac.
#
#   brew install cloudflared      # once
#   scripts/tunnel.sh             # Ctrl+C stops the tunnel and the public app
#
# Starts ONLY the public app (app.public: /health + TradingView webhooks) on
# 127.0.0.1:8001 and a Cloudflare quick tunnel to it. The internal API (portfolio,
# dashboard) on port 8000 is never exposed. The https://….trycloudflare.com address
# changes on every start and has no uptime guarantee: for testing only. Production
# runs on the VPS behind Caddy (deploy/DEPLOY.md).
set -eu
ROOT="${0:A:h:h}"
PY="$ROOT/.venv/bin/python"
PORT="${PUBLIC_PORT:-8001}"

if ! command -v cloudflared >/dev/null; then
  echo "cloudflared is not installed: brew install cloudflared"
  exit 1
fi

cd "$ROOT"
# No access log: webhook URLs carry a secret token. Real client IPs come from the
# tunnel's X-Forwarded-For, trusted only from the local cloudflared process.
"$PY" -m uvicorn app.public:app --host 127.0.0.1 --port "$PORT" --workers 1 \
  --no-access-log --proxy-headers --forwarded-allow-ips 127.0.0.1 &
APP=$!
trap 'kill $APP 2>/dev/null; wait $APP 2>/dev/null' EXIT INT TERM

for i in $(seq 1 20); do  # wait until the public app answers
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.5
done

echo "Public app on 127.0.0.1:$PORT. Look for the https://…trycloudflare.com line below."
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT"
