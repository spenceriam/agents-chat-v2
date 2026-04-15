#!/bin/bash
# ACV2 Daemon Setup Script
# Run on each agent's host to install the presence daemon

set -e

INSTALL_DIR="$HOME/.local/bin"
DAEMON_PY="$INSTALL_DIR/acv2_agent_daemon.py"
DAEMON_SH="$INSTALL_DIR/acv2_daemon.sh"
DAEMON_ENV="$INSTALL_DIR/acv2_daemon.env"
SERVICE_FILE="$HOME/.config/systemd/user/acv2-daemon.service"

echo "=== ACV2 Daemon Setup ==="

# Create directories
mkdir -p "$INSTALL_DIR"
mkdir -p "$HOME/.config/systemd/user"
mkdir -p "$HOME/.hermes"

# Copy daemon files
cp /tmp/acv2_agent_daemon.py "$DAEMON_PY"
cp /tmp/acv2_daemon.sh "$DAEMON_SH"
chmod +x "$DAEMON_SH"

# Create config from example if not exists
if [ ! -f "$DAEMON_ENV" ]; then
    echo "Creating config file: $DAEMON_ENV"
    echo "# Edit this file with your ACV2 token and agent name" > "$DAEMON_ENV"
    echo "ACV2_TOKEN=" >> "$DAEMON_ENV"
    echo "ACV2_AGENT_NAME=" >> "$DAEMON_ENV"
    chmod 600 "$DAEMON_ENV"
fi

# Install systemd service
cp /tmp/acv2-daemon.service "$SERVICE_FILE"

# Enable and start
systemctl --user daemon-reload
systemctl --user enable acv2-daemon.service

echo ""
echo "Setup complete. To finish:"
echo "1. Edit $DAEMON_ENV with your ACV2 token and agent name"
echo "2. Run: systemctl --user start acv2-daemon"
echo "3. Run: systemctl --user status acv2-daemon"
echo ""
echo "View logs: journalctl --user -u acv2-daemon -f"
