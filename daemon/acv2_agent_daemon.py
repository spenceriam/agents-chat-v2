#!/usr/bin/env python3
"""
ACV2 Agent Presence Daemon
Persistent WebSocket listener that monitors for DMs and mentions,
triggers agent gateway sessions when actionable messages arrive,
and handles simple autonomous responses via ACV2 HTTP API.

Designed to run as a systemd user service for all agents.
"""
import asyncio
import json
import logging
import os
import socket
import sys
import time
import signal
import subprocess
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.expanduser("~/.hermes/acv2_daemon.log")),
    ]
)
logger = logging.getLogger("acv2_daemon")

# Configuration from environment or defaults
ACV2_HOST = os.getenv("ACV2_HOST", "100.121.182.107")
ACV2_PORT = int(os.getenv("ACV2_PORT", "8081"))
AGENT_TOKEN = os.getenv("ACV2_TOKEN", "")
AGENT_NAME = os.getenv("ACV2_AGENT_NAME", "unknown")
GATEWAY_TRIGGER_URL = os.getenv("GATEWAY_TRIGGER_URL", "")  # e.g. http://localhost:8080/trigger
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "30"))
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))
MAX_RECONNECT_DELAY = int(os.getenv("MAX_RECONNECT_DELAY", "120"))

running = True

def signal_handler(sig, frame):
    global running
    logger.info(f"Received signal {sig}, shutting down...")
    running = False

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def get_ws_url():
    return f"ws://{ACV2_HOST}:{ACV2_PORT}/ws/v2/{AGENT_TOKEN}"


def get_api_base():
    return f"http://{ACV2_HOST}:{ACV2_PORT}/api/v2"


def send_acv2_message(room_or_dm, content, structured=None, is_dm=False):
    """Send a message back via ACV2 HTTP API."""
    if structured is None:
        structured = {"intent": "status"}
    
    if is_dm:
        url = f"{get_api_base()}/dms/{room_or_dm}/messages?token={AGENT_TOKEN}"
    else:
        url = f"{get_api_base()}/channels/{room_or_dm}/messages?token={AGENT_TOKEN}"
    
    payload = json.dumps({"content": content, "structured": structured}).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            logger.info(f"Sent ACV2 message: {result}")
            return result
    except Exception as e:
        logger.error(f"Failed to send ACV2 message: {e}")
        return None


def handle_server_restart_request(sender, msg_content):
    """Handle server restart requests autonomously."""
    logger.info(f"Processing server restart request from {sender}")
    
    # Parse the command from the message
    # Look for the restart command pattern
    restart_cmd = "cd /home/spencer/agents-chat-v2-build && git pull origin main && pkill -f main_v2.py && sleep 2 && nohup ./venv/bin/python main_v2.py > server.log 2>&1 & sleep 3 && curl -s http://localhost:8081/api/v2/health"
    
    try:
        # Execute the restart
        result = subprocess.run(
            restart_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        response = f"Server restart executed. Exit code: {result.returncode}\n"
        if result.stdout:
            response += f"stdout: {result.stdout[:500]}\n"
        if result.stderr:
            response += f"stderr: {result.stderr[:500]}\n"
        
        # Verify health
        try:
            with urlopen(f"http://localhost:8081/api/v2/health", timeout=5) as resp:
                health = resp.read().decode()
                response += f"Health check: {health}"
        except Exception as e:
            response += f"Health check failed: {e}"
        
        send_acv2_message(sender, response, structured={"intent": "task_complete"}, is_dm=True)
        
    except Exception as e:
        send_acv2_message(
            sender,
            f"Server restart failed: {e}",
            structured={"intent": "error"},
            is_dm=True
        )


def is_actionable_message(content):
    """Check if a message contains actionable commands."""
    actionable_keywords = [
        "restart", "server restart", "git pull", "pkill", "deploy",
        "task assignment", "TASK:", "PRIORITY:", "execute",
        "status check", "health check"
    ]
    lower = content.lower()
    return any(kw in lower for kw in actionable_keywords)


def extract_sender_and_command(content):
    """Extract who sent it and what they want."""
    # Simple heuristic - can be enhanced
    if "restart" in content.lower() and "server" in content.lower():
        return "server_restart"
    if "status" in content.lower() or "health" in content.lower():
        return "status_check"
    return "other"


async def run_daemon():
    """Main daemon loop."""
    logger.info(f"ACV2 Daemon starting for agent: {AGENT_NAME}")
    logger.info(f"ACV2 endpoint: {ACV2_HOST}:{ACV2_PORT}")
    logger.info(f"WebSocket: {get_ws_url()}")
    
    # Import websockets here since it might not be installed
    try:
        import websockets
    except ImportError:
        logger.error("websockets module not installed. Run: pip install websockets")
        sys.exit(1)
    
    reconnect_delay = RECONNECT_DELAY
    
    while running:
        try:
            logger.info(f"Connecting to ACV2 WebSocket...")
            
            async with websockets.connect(
                get_ws_url(),
                ping_interval=HEARTBEAT_INTERVAL,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                logger.info("Connected to ACV2 WebSocket successfully")
                reconnect_delay = RECONNECT_DELAY  # Reset on successful connect
                
                # Send initial presence
                await ws.send(json.dumps({
                    "action": "presence",
                    "agent": AGENT_NAME,
                    "status": "active"
                }))
                
                # Listen for messages
                async for message in ws:
                    try:
                        data = json.loads(message)
                        
                        # Handle different message types
                        msg_type = data.get("type", "")
                        
                        if msg_type == "message":
                            room_id = data.get("room_id", "")
                            sender = data.get("sender", "")
                            content = data.get("content", "")
                            timestamp = data.get("timestamp", "")
                            
                            logger.info(f"Message from {sender} in {room_id}: {content[:80]}...")
                            
                            # Skip our own messages
                            if sender == AGENT_NAME:
                                continue
                            
                            # Check if actionable
                            if is_actionable_message(content):
                                action = extract_sender_and_command(content)
                                logger.info(f"Actionable message detected: {action}")
                                
                                if action == "server_restart" and AGENT_NAME == "Data":
                                    # Data handles restarts
                                    handle_server_restart_request(sender, content)
                                elif action == "status_check":
                                    # Report status
                                    status = f"{AGENT_NAME} daemon active. Uptime: {datetime.now().isoformat()}"
                                    send_acv2_message(
                                        sender if sender != AGENT_NAME else "general",
                                        status,
                                        structured={"intent": "status"},
                                        is_dm=(room_id.startswith("dm_"))
                                    )
                                
                                # Also trigger gateway if configured
                                if GATEWAY_TRIGGER_URL:
                                    try:
                                        req = Request(
                                            GATEWAY_TRIGGER_URL,
                                            data=json.dumps({
                                                "source": "acv2",
                                                "sender": sender,
                                                "content": content,
                                                "room": room_id
                                            }).encode(),
                                            headers={"Content-Type": "application/json"},
                                            method="POST"
                                        )
                                        with urlopen(req, timeout=5) as resp:
                                            logger.info(f"Gateway triggered: {resp.status}")
                                    except Exception as e:
                                        logger.error(f"Failed to trigger gateway: {e}")
                        
                        elif msg_type == "ping":
                            await ws.send(json.dumps({"action": "pong"}))
                        
                        elif msg_type == "thread_created":
                            logger.info(f"Thread created: {data.get('thread', {}).get('topic', 'unknown')}")
                        
                        elif msg_type == "thread_message":
                            logger.info(f"Thread message received")
                    
                    except json.JSONDecodeError:
                        logger.warning(f"Non-JSON message received: {message[:100]}")
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                
                logger.warning("WebSocket connection closed")
        
        except websockets.exceptions.ConnectionClosedError as e:
            logger.warning(f"WebSocket closed: {e}")
        except ConnectionRefusedError:
            logger.error(f"Connection refused to {ACV2_HOST}:{ACV2_PORT}")
        except socket.gaierror:
            logger.error(f"DNS resolution failed for {ACV2_HOST}")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        
        if running:
            logger.info(f"Reconnecting in {reconnect_delay} seconds...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)


if __name__ == "__main__":
    if not AGENT_TOKEN:
        logger.error("ACV2_TOKEN environment variable not set")
        sys.exit(1)
    
    logger.info("Starting ACV2 Agent Presence Daemon")
    asyncio.run(run_daemon())
