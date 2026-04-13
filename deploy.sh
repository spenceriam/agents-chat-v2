#!/bin/bash
# Deployment script for agents-chat on bearcat.tech
# Run this on the VPS as root or with sudo

set -e

echo "=== Agent Chat Server Deployment ==="
echo "This script will install and configure the chat server"
echo ""

# Configuration
APP_DIR="/opt/agents-chat"
DOMAIN="${1:-chat.bearcat.tech}"

echo "Domain: $DOMAIN"
echo ""

# 1. Install system dependencies
echo "[1/7] Installing dependencies..."
apt update
apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx git

# 2. Setup application directory
echo "[2/7] Setting up application..."
if [ ! -d "$APP_DIR" ]; then
    mkdir -p "$APP_DIR"
    echo "Created $APP_DIR"
    echo "NOTE: Please clone the repository to $APP_DIR"
    echo "  git clone https://github.com/spenceriam/agents-chat.git $APP_DIR"
    exit 1
fi

# 3. Create virtual environment
echo "[3/7] Creating Python environment..."
cd "$APP_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt

# 4. Create agents.json if it doesn't exist
echo "[4/7] Checking configuration..."
if [ ! -f "agents.json" ]; then
    echo "Creating agents.json from template..."
    cp agents.json.example agents.json
    echo "⚠️  IMPORTANT: Edit $APP_DIR/agents.json with real tokens before starting!"
fi

# 5. Create systemd service
echo "[5/7] Installing systemd service..."
cp agents-chat.service /etc/systemd/system/
sed -i "s|/opt/agents-chat|$APP_DIR|g" /etc/systemd/system/agents-chat.service

# Create www-data user if doesn't exist
id -u www-data &>/dev/null || useradd -r -s /bin/false www-data

chown -R www-data:www-data "$APP_DIR"
systemctl daemon-reload
systemctl enable agents-chat

# 6. Configure Nginx
echo "[6/7] Configuring Nginx..."
cp nginx-agents-chat /etc/nginx/sites-available/agents-chat
sed -i "s/chat.bearcat.tech/$DOMAIN/g" /etc/nginx/sites-available/agents-chat

# Test if sites-enabled exists
if [ -d /etc/nginx/sites-enabled ]; then
    ln -sf /etc/nginx/sites-available/agents-chat /etc/nginx/sites-enabled/
else
    # For distros without sites-enabled
    cp /etc/nginx/sites-available/agents-chat /etc/nginx/conf.d/agents-chat.conf
fi

nginx -t && systemctl reload nginx

# 7. SSL certificate
echo "[7/7] Setting up SSL..."
echo "Attempting to get SSL certificate for $DOMAIN..."
echo "Make sure DNS points to this server first!"
echo ""

certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email admin@$DOMAIN 2>/dev/null || {
    echo "⚠️  SSL certificate failed. Possible reasons:"
    echo "   - DNS not yet propagated"
    echo "   - Domain doesn't point to this server"
    echo ""
    echo "To retry later: sudo certbot --nginx -d $DOMAIN"
}

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Next steps:"
echo "1. Edit $APP_DIR/agents.json with real agent tokens"
echo "2. Start the service: sudo systemctl start agents-chat"
echo "3. Check status: sudo systemctl status agents-chat"
echo "4. Access: https://$DOMAIN/?token=YOUR_TOKEN"
echo ""
echo "To generate tokens: python3 -c 'import uuid; print(uuid.uuid4())'"
