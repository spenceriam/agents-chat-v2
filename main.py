"""
Agent Chat Server - Web-based chat for AI agent coordination
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Configuration
DATABASE = "chat.db"
MAX_HISTORY = 100  # Messages to keep per room

# Token-to-agent mapping (configured by admin)
# Format: {"token-uuid": {"name": "AgentName", "color": "#hex"}}
AGENT_TOKENS = {}


def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_room_time ON messages(room, timestamp)
    ''')
    conn.commit()
    conn.close()


def load_agents():
    """Load agent tokens from config file"""
    global AGENT_TOKENS
    try:
        with open("agents.json", "r") as f:
            AGENT_TOKENS = json.load(f)
    except FileNotFoundError:
        # Default agents for testing
        AGENT_TOKENS = {
            "data-token-placeholder": {"name": "Data", "color": "#4A90E2"},
            "vega-token-placeholder": {"name": "Vega", "color": "#E74C3C"},
            "hermes-token-placeholder": {"name": "Hermes", "color": "#27AE60"},
            "tars-token-placeholder": {"name": "TARS", "color": "#F39C12"},
            "mandy-token-placeholder": {"name": "Mandy", "color": "#9B59B6"},
            "spencer-token-placeholder": {"name": "Spencer", "color": "#1ABC9C"},
        }
        with open("agents.json", "w") as f:
            json.dump(AGENT_TOKENS, f, indent=2)


def get_agent_by_token(token: str) -> Optional[dict]:
    """Get agent info from token"""
    return AGENT_TOKENS.get(token)


def save_message(room: str, sender: str, content: str):
    """Save message to database"""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (room, sender, content) VALUES (?, ?, ?)",
        (room, sender, content)
    )
    conn.commit()
    
    # Cleanup old messages
    cursor.execute(
        """DELETE FROM messages WHERE id NOT IN 
           (SELECT id FROM messages WHERE room = ? ORDER BY timestamp DESC LIMIT ?)""",
        (room, MAX_HISTORY)
    )
    conn.commit()
    conn.close()


def get_messages(room: str, limit: int = 50) -> List[dict]:
    """Get recent messages from room"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """SELECT sender, content, timestamp FROM messages 
           WHERE room = ? ORDER BY timestamp DESC LIMIT ?""",
        (room, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in reversed(rows)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_agents()
    yield


app = FastAPI(title="Agent Chat", lifespan=lifespan)

# Connection manager for WebSockets
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict = {}  # room -> list of (websocket, agent_name)
    
    async def connect(self, websocket: WebSocket, room: str, agent_name: str):
        await websocket.accept()
        if room not in self.active_connections:
            self.active_connections[room] = []
        self.active_connections[room].append((websocket, agent_name))
        # Broadcast join message
        await self.broadcast(room, {
            "type": "system",
            "content": f"{agent_name} joined",
            "timestamp": datetime.now().isoformat()
        })
    
    def disconnect(self, websocket: WebSocket, room: str):
        if room in self.active_connections:
            self.active_connections[room] = [
                (ws, name) for ws, name in self.active_connections[room] 
                if ws != websocket
            ]
    
    async def broadcast(self, room: str, message: dict):
        if room in self.active_connections:
            disconnected = []
            for websocket, _ in self.active_connections[room]:
                try:
                    await websocket.send_json(message)
                except:
                    disconnected.append(websocket)
            # Clean up disconnected
            for ws in disconnected:
                self.disconnect(ws, room)


manager = ConnectionManager()

# HTML Template for the chat interface
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Agent Chat - {{ agent_name }}</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .header {
            background: #16213e;
            padding: 1rem;
            border-bottom: 1px solid #0f3460;
        }
        .header h1 { font-size: 1.2rem; color: #e94560; }
        .header .agent { color: #888; font-size: 0.9rem; }
        .messages {
            flex: 1;
            overflow-y: auto;
            padding: 1rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }
        .message {
            max-width: 80%;
            padding: 0.75rem 1rem;
            border-radius: 1rem;
            word-wrap: break-word;
        }
        .message.own {
            align-self: flex-end;
            background: #0f3460;
        }
        .message.other {
            align-self: flex-start;
            background: #16213e;
            border: 1px solid #0f3460;
        }
        .message.system {
            align-self: center;
            background: transparent;
            color: #888;
            font-size: 0.85rem;
            font-style: italic;
        }
        .message .sender {
            font-size: 0.75rem;
            color: #e94560;
            margin-bottom: 0.25rem;
        }
        .message .time {
            font-size: 0.7rem;
            color: #666;
            margin-top: 0.25rem;
        }
        .input-area {
            padding: 1rem;
            background: #16213e;
            border-top: 1px solid #0f3460;
            display: flex;
            gap: 0.5rem;
        }
        .input-area input {
            flex: 1;
            padding: 0.75rem 1rem;
            border: none;
            border-radius: 0.5rem;
            background: #0f3460;
            color: #fff;
            font-size: 1rem;
        }
        .input-area input:focus { outline: 2px solid #e94560; }
        .input-area button {
            padding: 0.75rem 1.5rem;
            border: none;
            border-radius: 0.5rem;
            background: #e94560;
            color: #fff;
            font-size: 1rem;
            cursor: pointer;
        }
        .input-area button:hover { background: #ff6b6b; }
        .status {
            padding: 0.5rem 1rem;
            background: #0f3460;
            font-size: 0.85rem;
            color: #888;
        }
        .status.connected { color: #27ae60; }
        .status.disconnected { color: #e74c3c; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🤖 Agent Chat</h1>
        <div class="agent">Logged in as: <strong>{{ agent_name }}</strong></div>
    </div>
    <div class="status disconnected" id="status">● Disconnected</div>
    <div class="messages" id="messages"></div>
    <div class="input-area">
        <input type="text" id="messageInput" placeholder="Type a message..." autofocus>
        <button onclick="sendMessage()">Send</button>
    </div>

    <script>
        const token = "{{ token }}";
        const agentName = "{{ agent_name }}";
        const messagesDiv = document.getElementById('messages');
        const messageInput = document.getElementById('messageInput');
        const statusDiv = document.getElementById('status');
        let ws;

        function connect() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws/${token}`);
            
            ws.onopen = () => {
                statusDiv.textContent = '● Connected';
                statusDiv.className = 'status connected';
            };
            
            ws.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                displayMessage(msg);
            };
            
            ws.onclose = () => {
                statusDiv.textContent = '● Disconnected - Reconnecting...';
                statusDiv.className = 'status disconnected';
                setTimeout(connect, 3000);
            };
        }

        function displayMessage(msg) {
            const div = document.createElement('div');
            div.className = 'message';
            
            if (msg.type === 'system') {
                div.className += ' system';
                div.textContent = msg.content;
            } else {
                const isOwn = msg.sender === agentName;
                div.className += isOwn ? ' own' : ' other';
                div.innerHTML = `
                    <div class="sender">${msg.sender}</div>
                    <div>${escapeHtml(msg.content)}</div>
                    <div class="time">${formatTime(msg.timestamp)}</div>
                `;
            }
            
            messagesDiv.appendChild(div);
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }

        function sendMessage() {
            const content = messageInput.value.trim();
            if (!content || !ws) return;
            
            ws.send(JSON.stringify({ content }));
            messageInput.value = '';
        }

        messageInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') sendMessage();
        });

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function formatTime(timestamp) {
            const date = new Date(timestamp);
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }

        // Load history
        fetch(`/history?token=${token}`)
            .then(r => r.json())
            .then(messages => {
                messages.forEach(msg => displayMessage({...msg, type: 'chat'}));
            });

        connect();
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def chat_page(token: str):
    """Main chat interface"""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    from jinja2 import Template
    template = Template(HTML_TEMPLATE)
    return template.render(agent_name=agent["name"], token=token)


@app.get("/history")
async def get_history(token: str):
    """Get message history (for initial load)"""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    messages = get_messages("main", 50)
    return messages


@app.post("/send")
async def send_http_message(token: str, content: str):
    """Send message via HTTP POST (for agents without WebSocket support)"""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    save_message("main", agent["name"], content)
    
    message = {
        "type": "chat",
        "sender": agent["name"],
        "content": content,
        "timestamp": datetime.now().isoformat()
    }
    
    await manager.broadcast("main", message)
    return {"status": "sent"}


@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    """WebSocket endpoint for real-time chat"""
    agent = get_agent_by_token(token)
    if not agent:
        await websocket.close(code=403)
        return
    
    await manager.connect(websocket, "main", agent["name"])
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg_data = json.loads(data)
                content = msg_data.get("content", "").strip()
                if content:
                    save_message("main", agent["name"], content)
                    await manager.broadcast("main", {
                        "type": "chat",
                        "sender": agent["name"],
                        "content": content,
                        "timestamp": datetime.now().isoformat()
                    })
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket, "main")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
