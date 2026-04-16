#!/bin/bash
# ACV2 Deploy Script - Pulls latest code and restarts the server
# Run on jefferies (100.121.182.107)
set -e

ACV2_DIR="/home/spencer/agents-chat-v2"
SERVICE_NAME="acv2-server"
LOG_FILE="$ACV2_DIR/server.log"

echo "=== ACV2 Deploy ==="
echo "[$(date -Iseconds)] Starting deploy..."

cd "$ACV2_DIR"

# Pull latest code
echo "[1/4] Pulling latest code..."
git pull origin main

# Install dependencies if requirements changed
echo "[2/4] Checking dependencies..."
"$ACV2_DIR/venv/bin/pip" install -q -r requirements.txt

# Install/update systemd service
echo "[3/4] Installing systemd service..."
sudo cp "$ACV2_DIR/agents-chat-v2.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload

# Restart server
echo "[4/4] Restarting server..."
sudo systemctl restart "$SERVICE_NAME"

sleep 2

# Verify
if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo "✅ ACV2 server restarted successfully."
    echo "   Status: $(sudo systemctl status "$SERVICE_NAME" --no-pager | head -3)"
    echo "   Health: $(curl -s http://localhost:8081/api/v2/health | python3 -m json.tool)"
else
    echo ""
    echo "❌ Server failed to start. Check logs:"
    echo "   sudo journalctl -u $SERVICE_NAME -n 20 --no-pager"
    exit 1
fi
