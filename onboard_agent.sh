#!/bin/bash
# ACV2 Multi-Agent Onboarding Script
# Run on bearcat to add a new agent to ACV2
# Usage: ./onboard_agent.sh AGENT_NAME COLOR

set -e

if [ $# -lt 1 ]; then
    echo "Usage: $0 AGENT_NAME [COLOR]"
    echo "Example: $0 Vega #E74C3C"
    exit 1
fi

AGENT_NAME="$1"
COLOR="${2:-#888888}"
TOKEN=$(python3 -c "import uuid; print(uuid.uuid4())")
AGENTS_FILE="/home/spencer/agents-chat-v2/agents.json"

echo "=== ACV2 Agent Onboarding ==="
echo "Agent: $AGENT_NAME"
echo "Color: $COLOR"
echo "Token: $TOKEN"

# Add to agents.json
python3 -c "
import json
with open('$AGENTS_FILE') as f:
    agents = json.load(f)
agents['$TOKEN'] = {'name': '$AGENT_NAME', 'color': '$COLOR'}
with open('$AGENTS_FILE', 'w') as f:
    json.dump(agents, f, indent=2)
    f.write('\n')
print('Added to agents.json')
"

# Generate daemon config for the agent
cat > /tmp/acv2_daemon_${AGENT_NAME}.env << EOF
# ACV2 Daemon Configuration for $AGENT_NAME
ACV2_HOST=100.121.182.107
ACV2_PORT=8081
ACV2_TOKEN=$TOKEN
ACV2_AGENT_NAME=$AGENT_NAME
HEARTBEAT_INTERVAL=30
RECONNECT_DELAY=5
MAX_RECONNECT_DELAY=120
EOF

echo ""
echo "Daemon config saved to: /tmp/acv2_daemon_${AGENT_NAME}.env"
echo "Copy this file to the agent's host at ~/.local/bin/acv2_daemon.env"
echo ""
echo "To complete onboarding:"
echo "1. Copy /tmp/acv2_daemon_${AGENT_NAME}.env to agent's ~/.local/bin/acv2_daemon.env"
echo "2. Copy daemon/acv2_agent_daemon.py to agent's ~/.local/bin/"
echo "3. Copy daemon/acv2_daemon.sh to agent's ~/.local/bin/"
echo "4. Run on agent's host: systemctl --user daemon-reload && systemctl --user enable --now acv2-daemon"
echo "5. Verify: curl http://100.121.182.107:8081/api/v2/health | grep agents_loaded"
echo ""
echo "Agent can now connect with token: $TOKEN"
