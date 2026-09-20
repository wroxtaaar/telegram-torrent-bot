#!/bin/sh
set -eu

# Render Web Service: qBittorrent + Telegram bot + download server.
# qBittorrent listens only on localhost:8080; the aiohttp server uses $PORT.

mkdir -p /downloads /config/qbittorrent

# qBittorrent config for headless WebUI.
cat > /config/qbittorrent/qBittorrent.conf <<EOF
[Preferences]
WebUI\\Enabled=true
WebUI\\Address=127.0.0.1
WebUI\\Port=8080
WebUI\\LocalHostAuth=false
WebUI\\AuthSubnetWhitelistEnabled=true
WebUI\\AuthSubnetWhitelist=127.0.0.0/8
Downloads\\SavePath=/downloads/
Downloads\\TempPath=/downloads/.incomplete/
Downloads\\TempPathEnabled=true
Session\\DefaultSavePath=/downloads/
Session\\Port=6881
EOF

# Start qBittorrent in the background.
qbittorrent-nox --profile=/config/qbittorrent --webui-port=8080 &
QBIT_PID=$!

cleanup() {
  kill "$QBIT_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Give the WebUI a moment to initialize.
sleep 3

# The bot needs Render's public URL for generated file links.
export QBIT_HOST="127.0.0.1"
export QBIT_PORT="8080"
export DOWNLOAD_DIR="/downloads"
export WEB_PORT="${PORT:-8080}"

python -m web.server &
WEB_PID=$!

python -m bot.bot &
BOT_PID=$!

wait -n "$QBIT_PID" "$WEB_PID" "$BOT_PID"
exit $?
