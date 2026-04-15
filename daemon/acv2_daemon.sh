#!/bin/bash
# ACV2 Daemon wrapper - loads agent-specific config
# Install to: /home/spencer/.local/bin/acv2_daemon.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/acv2_daemon.env"

if [ -f "$CONFIG_FILE" ]; then
    set -a
    source "$CONFIG_FILE"
    set +a
fi

exec python3 "${SCRIPT_DIR}/acv2_agent_daemon.py"
