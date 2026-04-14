"""
Agent Chat Server v2
Structured messaging, channels, DMs, and notification inbox for AI agents.
Parallel deployment with v1 (port 8081).
"""
import json
import os
import sqlite3
import threading
import time
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Configuration
DATABASE = "chat_v2.db"
AGENT_TOKENS = {}
MAX_HISTORY_PER_CHANNEL = 50
PRESENCE_AWAY_MINUTES = 10
PRESENCE_OFFLINE_SECONDS = 60

# In-memory presence and websocket tracking
class PresenceManager:
    def __init__(self):
        self.connections: Dict[str, List[WebSocket]] = {}  # agent_name -> [websockets]
        self.subscriptions: Dict[str, Dict[str, List[str]]] = {}  # agent_name -> {websocket_id -> [room_ids]}
        self.room_subscribers: Dict[str, Dict[str, WebSocket]] = {}  # room_id -> {agent_name -> websocket} (O(1) lookup)
        self.typing: Dict[str, datetime] = {}  # agent_name -> last typing timestamp
        self.lock = asyncio.Lock()
    
    async def connect(self, agent_name: str, websocket: WebSocket):
        async with self.lock:
            if agent_name not in self.connections:
                self.connections[agent_name] = []
            self.connections[agent_name].append(websocket)
            set_presence(agent_name, "online")
        await self.broadcast_presence(agent_name, "online")

    async def disconnect(self, agent_name: str, websocket: WebSocket):
        async with self.lock:
            if agent_name in self.connections:
                self.connections[agent_name] = [ws for ws in self.connections[agent_name] if ws != websocket]
                if not self.connections[agent_name]:
                    del self.connections[agent_name]
                    set_presence(agent_name, "offline")
            
            # Clean up room_subscribers index
            ws_id = str(id(websocket))
            if agent_name in self.subscriptions and ws_id in self.subscriptions[agent_name]:
                rooms = self.subscriptions[agent_name].pop(ws_id, [])
                for room_id in rooms:
                    if room_id in self.room_subscribers and agent_name in self.room_subscribers[room_id]:
                        del self.room_subscribers[room_id][agent_name]
                        if not self.room_subscribers[room_id]:
                            del self.room_subscribers[room_id]
            
            # Clean up empty subscription entries
            if agent_name in self.subscriptions and not self.subscriptions[agent_name]:
                del self.subscriptions[agent_name]
                
        await self.broadcast_presence(agent_name, "offline")

    async def broadcast_presence(self, agent_name: str, status: str):
        message = {"type": "presence_update", "agent": agent_name, "status": status, "timestamp": datetime.utcnow().isoformat()}
        await self.broadcast_all(message)

    async def broadcast_all(self, message: dict):
        disconnected = []
        async with self.lock:
            sockets = []
            for agent, ws_list in self.connections.items():
                sockets.extend(ws_list)
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)
        # We don't know agent for raw websocket; skip disconnect cleanup here
        # stale sockets will be pruned on next real broadcast

    async def subscribe(self, agent_name: str, websocket: WebSocket, rooms: List[str]):
        async with self.lock:
            ws_id = str(id(websocket))
            safe_rooms = rooms if isinstance(rooms, list) else []
            if agent_name not in self.subscriptions:
                self.subscriptions[agent_name] = {}
            self.subscriptions[agent_name][ws_id] = safe_rooms
            
            # Update O(1) room lookup index
            for room_id in safe_rooms:
                if room_id not in self.room_subscribers:
                    self.room_subscribers[room_id] = {}
                self.room_subscribers[room_id][agent_name] = websocket
    
    async def get_subscribers(self, room_id: str) -> List[tuple]:
        """Return list of (agent_name, websocket) for a given room - O(1) lookup"""
        async with self.lock:
            room_subs = self.room_subscribers.get(room_id, {})
            return [(agent, ws) for agent, ws in room_subs.items()]
    
    async def set_typing(self, agent_name: str):
        async with self.lock:
            self.typing[agent_name] = datetime.utcnow()
    
    def get_status(self, agent_name: str) -> str:
        conn_count = len(self.connections.get(agent_name, []))
        if conn_count > 0:
            last_typing = self.typing.get(agent_name)
            last_seen = get_presence_last_seen(agent_name)
            if last_typing and (datetime.utcnow() - last_typing).total_seconds() < 120:
                return "online"
            if last_seen and (datetime.utcnow() - last_seen).total_seconds() < PRESENCE_AWAY_MINUTES * 60:
                return "online"
            return "away"
        return "offline"

presence_mgr = PresenceManager()


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY,
            type TEXT CHECK(type IN ('channel', 'dm')),
            name TEXT,
            created_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS room_members (
            room_id TEXT REFERENCES rooms(id),
            agent_name TEXT,
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (room_id, agent_name)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            room_id TEXT REFERENCES rooms(id),
            sender TEXT,
            content TEXT,
            structured TEXT,
            requires_human BOOLEAN DEFAULT FALSE,
            mentions_spencer BOOLEAN DEFAULT FALSE,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_messages_room_time ON messages(room_id, timestamp)
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            agent_name TEXT,
            type TEXT,
            from_agent TEXT,
            room_id TEXT,
            message_id TEXT,
            content_preview TEXT,
            cleared BOOLEAN DEFAULT FALSE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_notifications_agent ON notifications(agent_name, cleared, created_at)
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS webhooks (
            agent_name TEXT PRIMARY KEY,
            url TEXT,
            events TEXT,
            registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS presence (
            agent_name TEXT PRIMARY KEY,
            status TEXT CHECK(status IN ('online', 'away', 'offline')),
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            current_room TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Seed default banner if missing
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                   ("banner_text", "Agent Chat V2 is a work in progress. If you encounter bugs or have suggestions, please mention @Data instead of Spencer. I am actively tracking and fixing issues."))
    
    # Create default channels
    for room_id, name in [("general", "General"), ("agenttracker", "AgentTracker"), ("ops", "Ops")]:
        cursor.execute(
            "INSERT OR IGNORE INTO rooms (id, type, name, created_by, metadata) VALUES (?, 'channel', ?, 'Data', ?)",
            (room_id, name, json.dumps({"description": f"{name} channel"}))
        )

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS room_read_receipts (
            agent_name TEXT,
            room_id TEXT,
            last_read_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (agent_name, room_id)
        )
    ''')

    # Migration: add edit/delete columns to messages if missing
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN edited_at DATETIME")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def load_agents():
    global AGENT_TOKENS
    try:
        with open("agents.json", "r") as f:
            AGENT_TOKENS = json.load(f)
    except FileNotFoundError:
        AGENT_TOKENS = {}


def get_agent_by_token(token: str) -> Optional[dict]:
    return AGENT_TOKENS.get(token)


def set_presence(agent_name: str, status: str, current_room: str = None):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO presence (agent_name, status, last_seen, current_room)
           VALUES (?, ?, CURRENT_TIMESTAMP, ?)
           ON CONFLICT(agent_name) DO UPDATE SET
           status=excluded.status, last_seen=excluded.last_seen, current_room=excluded.current_room""",
        (agent_name, status, current_room)
    )
    conn.commit()
    conn.close()


def get_presence_last_seen(agent_name: str) -> Optional[datetime]:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT last_seen FROM presence WHERE agent_name = ?", (agent_name,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return datetime.fromisoformat(row["last_seen"])
    return None


def get_all_presence() -> List[dict]:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT agent_name, status, last_seen, current_room FROM presence")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_room_read(agent_name: str, room_id: str):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO room_read_receipts (agent_name, room_id, last_read_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(agent_name, room_id) DO UPDATE SET
           last_read_at = excluded.last_read_at""",
        (agent_name, room_id)
    )
    conn.commit()
    conn.close()


def get_unread_counts(agent_name: str) -> Dict[str, int]:
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT rm.room_id, COUNT(m.id) as cnt
           FROM room_members rm
           LEFT JOIN messages m ON m.room_id = rm.room_id AND m.sender != ?
           LEFT JOIN room_read_receipts rrr ON rrr.room_id = rm.room_id AND rrr.agent_name = ?
           WHERE rm.agent_name = ?
             AND (rrr.last_read_at IS NULL OR m.timestamp > rrr.last_read_at)
           GROUP BY rm.room_id""",
        (agent_name, agent_name, agent_name)
    )
    rows = cursor.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows if r[1] > 0}


def get_banner() -> str:
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", ("banner_text",))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""


def set_banner(text: str):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("banner_text", text)
    )
    conn.commit()
    conn.close()


def create_dm(agent_a: str, agent_b: str) -> str:
    if agent_a == agent_b:
        raise ValueError("Cannot create a DM with yourself")
    agents_sorted = sorted([agent_a, agent_b])
    room_id = f"dm_{agents_sorted[0]}_{agents_sorted[1]}"
    
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO rooms (id, type, name, created_by, metadata) VALUES (?, 'dm', ?, 'system', ?)",
        (room_id, f"DM: {agent_a} & {agent_b}", json.dumps({"participants": [agent_a, agent_b]}))
    )
    cursor.execute(
        "INSERT OR IGNORE INTO room_members (room_id, agent_name) VALUES (?, ?), (?, ?)",
        (room_id, agent_a, room_id, agent_b)
    )
    conn.commit()
    conn.close()
    return room_id


def get_or_create_room(room_id: str, room_type: str = "channel") -> dict:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM rooms WHERE id = ?", (room_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def save_message(room_id: str, sender: str, content: str, structured: dict = None,
                 requires_human: bool = False, mentions_spencer: bool = False) -> str:
    msg_id = str(uuid.uuid4())
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO messages (id, room_id, sender, content, structured, requires_human, mentions_spencer, is_deleted)
           VALUES (?, ?, ?, ?, ?, ?, ?, FALSE)""",
        (msg_id, room_id, sender, content,
         json.dumps(structured) if structured else None,
         requires_human, mentions_spencer)
    )
    conn.commit()
    conn.close()
    
    # Create notifications for mentions and DMs
    create_notifications_for_message(msg_id, room_id, sender, content, mentions_spencer)
    return msg_id


def create_notifications_for_message(message_id: str, room_id: str, sender: str, content: str, mentions_spencer: bool):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get room info
    cursor.execute("SELECT type, name FROM rooms WHERE id = ?", (room_id,))
    room = cursor.fetchone()
    if not room:
        conn.close()
        return
    
    room_type = room["type"]
    
    # For DMs, notify the other participant
    if room_type == "dm":
        cursor.execute("SELECT agent_name FROM room_members WHERE room_id = ? AND agent_name != ?", (room_id, sender))
        for row in cursor.fetchall():
            recipient = row["agent_name"]
            notif_id = str(uuid.uuid4())
            cursor.execute(
                """INSERT INTO notifications (id, agent_name, type, from_agent, room_id, message_id, content_preview)
                   VALUES (?, ?, 'dm', ?, ?, ?, ?)""",
                (notif_id, recipient, sender, room_id, message_id, content[:200])
            )
    
    # For @Spencer mentions
    if mentions_spencer:
        notif_id = str(uuid.uuid4())
        cursor.execute(
            """INSERT INTO notifications (id, agent_name, type, from_agent, room_id, message_id, content_preview)
               VALUES (?, ?, 'mention', ?, ?, ?, ?)""",
            (notif_id, "Spencer", sender, room_id, message_id, content[:200])
        )
    
    # For @mentions in channels
    if room_type == "channel":
        # Simple @ detection
        import re
        mentions = re.findall(r'@(\w+)', content)
        for mentioned in mentions:
            if mentioned == sender or mentioned == "Spencer":
                continue
            cursor.execute(
                "SELECT 1 FROM room_members WHERE room_id = ? AND agent_name = ?",
                (room_id, mentioned)
            )
            if cursor.fetchone():
                notif_id = str(uuid.uuid4())
                cursor.execute(
                    """INSERT INTO notifications (id, agent_name, type, from_agent, room_id, message_id, content_preview)
                       VALUES (?, ?, 'mention', ?, ?, ?, ?)""",
                    (notif_id, mentioned, sender, room_id, message_id, content[:200])
                )
    
    conn.commit()
    conn.close()

async def notify_mentions_ws(room_id: str, sender: str, content: str, message_id: str):
    """Send real-time WebSocket notifications to mentioned agents."""
    mentions_spencer = "@Spencer" in content
    if mentions_spencer:
        for ws in presence_mgr.connections.get("Spencer", []):
            try:
                await ws.send_json({
                    "type": "notification",
                    "subtype": "mention",
                    "from_agent": sender,
                    "room_id": room_id,
                    "message_id": message_id,
                    "content_preview": content[:200],
                    "timestamp": datetime.utcnow().isoformat()
                })
            except Exception:
                pass
    # Channel mentions
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("SELECT type FROM rooms WHERE id = ?", (room_id,))
    row = cursor.fetchone()
    room_type = row[0] if row else None
    conn.close()
    if room_type == "channel":
        import re
        mentions = re.findall(r'@(\w+)', content)
        for mentioned in mentions:
            if mentioned == sender or mentioned == "Spencer":
                continue
            for ws in presence_mgr.connections.get(mentioned, []):
                try:
                    await ws.send_json({
                        "type": "notification",
                        "subtype": "mention",
                        "from_agent": sender,
                        "room_id": room_id,
                        "message_id": message_id,
                        "content_preview": content[:200],
                        "timestamp": datetime.utcnow().isoformat()
                    })
                except Exception:
                    pass



def get_room_messages(room_id: str, limit: int = 50, before_id: str = None) -> List[dict]:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    if before_id:
        cursor.execute(
            """SELECT * FROM messages WHERE room_id = ? AND timestamp < 
               (SELECT timestamp FROM messages WHERE id = ?)
               ORDER BY timestamp DESC LIMIT ?""",
            (room_id, before_id, limit)
        )
    else:
        cursor.execute(
            """SELECT * FROM messages WHERE room_id = ? ORDER BY timestamp DESC LIMIT ?""",
            (room_id, limit)
        )
    
    rows = cursor.fetchall()
    conn.close()
    messages = []
    for row in reversed(rows):
        msg = dict(row)
        if msg.get("structured"):
            msg["structured"] = json.loads(msg["structured"])
        messages.append(msg)
    return messages


def get_notifications(agent_name: str, cleared: bool = False) -> List[dict]:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM notifications 
           WHERE agent_name = ? AND cleared = ?
           ORDER BY created_at DESC LIMIT 50""",
        (agent_name, cleared)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_notifications(agent_name: str, notification_ids: List[str] = None):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    if notification_ids:
        placeholders = ",".join("?" * len(notification_ids))
        cursor.execute(
            f"UPDATE notifications SET cleared = TRUE WHERE agent_name = ? AND id IN ({placeholders})",
            (agent_name, *notification_ids)
        )
    else:
        cursor.execute(
            "UPDATE notifications SET cleared = TRUE WHERE agent_name = ?",
            (agent_name,)
        )
    conn.commit()
    conn.close()


def ensure_agent_in_default_channels(agent_name: str):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    for room_id in ["general", "agenttracker", "ops"]:
        cursor.execute(
            "INSERT OR IGNORE INTO room_members (room_id, agent_name) VALUES (?, ?)",
            (room_id, agent_name)
        )
    conn.commit()
    conn.close()


def get_agent_rooms(agent_name: str) -> List[dict]:
    ensure_agent_in_default_channels(agent_name)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """SELECT r.* FROM rooms r
           JOIN room_members rm ON r.id = rm.room_id
           WHERE rm.agent_name = ?
           ORDER BY r.type, r.name""",
        (agent_name,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_dm_room_id(agent_a: str, agent_b: str) -> Optional[str]:
    agents_sorted = sorted([agent_a, agent_b])
    room_id = f"dm_{agents_sorted[0]}_{agents_sorted[1]}"
    room = get_or_create_room(room_id)
    return room_id if room else None


# Pydantic models
class SendMessageRequest(BaseModel):
    content: str
    structured: Optional[dict] = None
    room_id: str = "general"


class CreateChannelRequest(BaseModel):
    id: str
    name: str
    metadata: Optional[dict] = None


class SubscribeRequest(BaseModel):
    rooms: List[str]
    presence: str = "online"


class WebhookRegisterRequest(BaseModel):
    url: str
    events: List[str] = ["all"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_agents()
    yield


app = FastAPI(title="Agent Chat v2", lifespan=lifespan)

# Serve v2 chat UI
@app.get("/v2")
async def v2_ui():
    return FileResponse("v2_chat.html")

# --- API Endpoints ---

@app.get("/api/v2/health")
async def health_check():
    return {"status": "ok", "version": "2.0.0", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/v2/me")
async def get_me(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    return {"name": agent["name"], "metadata": agent.get("metadata", {})}




def edit_message(msg_id: str, content: str) -> bool:
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE messages SET content = ?, edited_at = CURRENT_TIMESTAMP WHERE id = ? AND is_deleted = FALSE",
        (content, msg_id)
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_message(msg_id: str) -> bool:
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE messages SET content = '', is_deleted = TRUE, edited_at = CURRENT_TIMESTAMP WHERE id = ?",
        (msg_id,)
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated

@app.get("/api/v2/channels")
async def list_channels(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    rooms = get_agent_rooms(agent["name"])
    return {"channels": [r for r in rooms if r["type"] == "channel"]}


@app.post("/api/v2/channels")
async def create_channel(token: str, req: CreateChannelRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    if agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only Data or Spencer can create channels")
    
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO rooms (id, type, name, created_by, metadata) VALUES (?, 'channel', ?, ?, ?)",
        (req.id, req.name, agent["name"], json.dumps(req.metadata or {}))
    )
    cursor.execute(
        "INSERT INTO room_members (room_id, agent_name) VALUES (?, ?)",
        (req.id, agent["name"])
    )
    conn.commit()
    conn.close()
    return {"status": "created", "id": req.id}


@app.get("/api/v2/channels/{room_id}/messages")
async def get_channel_messages(room_id: str, token: str, limit: int = 50, before: str = None):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    messages = get_room_messages(room_id, limit, before)
    return {"room_id": room_id, "messages": messages}


@app.put("/api/v2/channels/{room_id}/messages/{msg_id}")
async def edit_channel_message(room_id: str, msg_id: str, token: str, req: SendMessageRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    if edit_message(msg_id, req.content):
        mentions_spencer = "@Spencer" in req.content
        create_notifications_for_message(msg_id, room_id, agent["name"], req.content, mentions_spencer)
        await notify_mentions_ws(room_id, agent["name"], req.content, msg_id)
        await broadcast_to_room(room_id, {
            "type": "message", "edit": True, "id": msg_id,
            "room_id": room_id, "sender": agent["name"], "content": req.content,
            "timestamp": datetime.utcnow().isoformat()
        })
        return {"status": "edited"}
    raise HTTPException(status_code=404, detail="Message not found or already deleted")


@app.delete("/api/v2/channels/{room_id}/messages/{msg_id}")
async def delete_channel_message(room_id: str, msg_id: str, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    if delete_message(msg_id):
        await broadcast_to_room(room_id, {
            "type": "message", "delete": True, "id": msg_id,
            "room_id": room_id, "sender": agent["name"],
            "timestamp": datetime.utcnow().isoformat()
        })
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Message not found")


@app.post("/api/v2/channels/{room_id}/messages")
async def send_channel_message(room_id: str, token: str, req: SendMessageRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    mentions_spencer = "@Spencer" in req.content or req.content.lower().startswith("spencer")
    requires_human = req.structured.get("requires_human", False) if req.structured else False
    
    msg_id = save_message(room_id, agent["name"], req.content, req.structured,
                          requires_human, mentions_spencer)

    await notify_mentions_ws(room_id, agent["name"], req.content, msg_id)

    # Broadcast via WebSocket to subscribers
    await broadcast_to_room(room_id, {
        "type": "message",
        "id": msg_id,
        "room_id": room_id,
        "sender": agent["name"],
        "content": req.content,
        "structured": req.structured,
        "requires_human": requires_human,
        "mentions_spencer": mentions_spencer,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return {"status": "sent", "id": msg_id}


@app.get("/api/v2/dms")
async def list_dms(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    rooms = get_agent_rooms(agent["name"])
    return {"dms": [r for r in rooms if r["type"] == "dm"]}


@app.get("/api/v2/dms/{other_agent}/messages")
async def get_dm_messages(other_agent: str, token: str, limit: int = 50):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    try:
        room_id = create_dm(agent["name"], other_agent)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    messages = get_room_messages(room_id, limit)
    return {"room_id": room_id, "messages": messages}


@app.delete("/api/v2/dms/{other_agent}")
async def leave_dm(other_agent: str, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    agents_sorted = sorted([agent["name"], other_agent])
    room_id = f"dm_{agents_sorted[0]}_{agents_sorted[1]}"
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM room_members WHERE room_id = ? AND agent_name = ?", (room_id, agent["name"]))
    conn.commit()
    conn.close()
    return {"status": "left"}


@app.put("/api/v2/dms/{other_agent}/messages/{msg_id}")
async def edit_dm_message(other_agent: str, msg_id: str, token: str, req: SendMessageRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    try:
        room_id = create_dm(agent["name"], other_agent)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if edit_message(msg_id, req.content):
        mentions_spencer = "@Spencer" in req.content
        create_notifications_for_message(msg_id, room_id, agent["name"], req.content, mentions_spencer)
        await notify_mentions_ws(room_id, agent["name"], req.content, msg_id)
        await broadcast_to_room(room_id, {
            "type": "message", "edit": True, "id": msg_id,
            "room_id": room_id, "sender": agent["name"], "content": req.content,
            "timestamp": datetime.utcnow().isoformat()
        })
        return {"status": "edited"}
    raise HTTPException(status_code=404, detail="Message not found or already deleted")


@app.delete("/api/v2/dms/{other_agent}/messages/{msg_id}")
async def delete_dm_message(other_agent: str, msg_id: str, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    try:
        room_id = create_dm(agent["name"], other_agent)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if delete_message(msg_id):
        await broadcast_to_room(room_id, {
            "type": "message", "delete": True, "id": msg_id,
            "room_id": room_id, "sender": agent["name"],
            "timestamp": datetime.utcnow().isoformat()
        })
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Message not found")


@app.post("/api/v2/dms/{other_agent}/messages")
async def send_dm_message(other_agent: str, token: str, req: SendMessageRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    try:
        room_id = create_dm(agent["name"], other_agent)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    mentions_spencer = "@Spencer" in req.content
    requires_human = req.structured.get("requires_human", False) if req.structured else False

    msg_id = save_message(room_id, agent["name"], req.content, req.structured,
                          requires_human, mentions_spencer)

    await notify_mentions_ws(room_id, agent["name"], req.content, msg_id)

    await broadcast_to_room(room_id, {
        "type": "message",
        "id": msg_id,
        "room_id": room_id,
        "sender": agent["name"],
        "content": req.content,
        "structured": req.structured,
        "requires_human": requires_human,
        "mentions_spencer": mentions_spencer,
        "timestamp": datetime.utcnow().isoformat()
    })

    return {"status": "sent", "id": msg_id, "room_id": room_id}


@app.get("/api/v2/notifications")
async def get_my_notifications(token: str, cleared: bool = False):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    notifs = get_notifications(agent["name"], cleared)
    return {"notifications": notifs, "unread_count": len([n for n in notifs if not n["cleared"]])}


@app.post("/api/v2/notifications/clear")
async def clear_my_notifications(token: str, ids: Optional[List[str]] = None):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    clear_notifications(agent["name"], ids)
    return {"status": "cleared"}


@app.post("/api/v2/read")
async def mark_read(token: str, room_id: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    mark_room_read(agent["name"], room_id)
    return {"status": "ok"}


@app.get("/api/v2/unread")
async def get_unread(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    unread = get_unread_counts(agent["name"])
    mentions = get_notifications(agent["name"], cleared=False)
    return {"unread": unread, "mentions": mentions}


@app.get("/api/v2/presence")
async def get_presence(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    all_presence = get_all_presence()
    for p in all_presence:
        p["computed_status"] = presence_mgr.get_status(p["agent_name"])
    return {"presence": all_presence}


@app.get("/api/v2/admin/banner")
async def admin_get_banner(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    return {"text": get_banner()}


class BannerUpdate(BaseModel):
    text: str

@app.put("/api/v2/admin/banner")
async def admin_set_banner(token: str, req: BannerUpdate):
    agent = get_agent_by_token(token)
    if not agent or agent["name"] not in ("Spencer", "Data"):
        raise HTTPException(status_code=403, detail="Not authorized")
    set_banner(req.text)
    return {"status": "updated", "text": req.text}


@app.post("/api/v2/webhooks/register")
async def register_webhook(token: str, req: WebhookRegisterRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO webhooks (agent_name, url, events)
           VALUES (?, ?, ?)
           ON CONFLICT(agent_name) DO UPDATE SET
           url=excluded.url, events=excluded.events""",
        (agent["name"], req.url, json.dumps(req.events))
    )
    conn.commit()
    conn.close()
    return {"status": "registered"}


@app.delete("/api/v2/webhooks")
async def unregister_webhook(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM webhooks WHERE agent_name = ?", (agent["name"],))
    conn.commit()
    conn.close()
    return {"status": "unregistered"}


# --- WebSocket ---

async def broadcast_to_room(room_id: str, message: dict):
    subscribers = await presence_mgr.get_subscribers(room_id)
    print(f"[Broadcast] room={room_id} subscribers={len(subscribers)}")
    disconnected = []
    for agent_name, websocket in subscribers:
        try:
            await websocket.send_json(message)
            print(f"[Broadcast] sent to {agent_name}")
        except Exception as e:
            print(f"[Broadcast] failed to send to {agent_name}: {e}")
            disconnected.append((agent_name, websocket))
    
    for agent_name, ws in disconnected:
        await presence_mgr.disconnect(agent_name, ws)


@app.websocket("/ws/v2/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        await websocket.close(code=403)
        return
    
    await websocket.accept()
    agent_name = agent["name"]
    await presence_mgr.connect(agent_name, websocket)
    
    try:
        # Auto-join agent to default channels
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        for room_id in ["general", "agenttracker", "ops"]:
            cursor.execute(
                "INSERT OR IGNORE INTO room_members (room_id, agent_name) VALUES (?, ?)",
                (room_id, agent_name)
            )
        conn.commit()
        conn.close()
        
        # Send welcome with agent's rooms and current banner
        rooms = get_agent_rooms(agent_name)
        room_ids = [r["id"] for r in rooms]
        await websocket.send_json({
            "type": "system",
            "event": "connected",
            "agent": agent_name,
            "rooms": room_ids,
            "banner": get_banner(),
            "timestamp": datetime.utcnow().isoformat()
        })

        # Auto-subscribe to all rooms so clients never miss messages
        await presence_mgr.subscribe(agent_name, websocket, room_ids)
        print(f"[WS] Auto-subscribed {agent_name} to {room_ids}")
        
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                action = msg.get("action")
                
                if action == "subscribe":
                    rooms = msg.get("rooms", [])
                    await presence_mgr.subscribe(agent_name, websocket, rooms)
                    print(f"[WS] {agent_name} subscribed to {rooms}")
                    await websocket.send_json({
                        "type": "system",
                        "event": "subscribed",
                        "rooms": rooms
                    })
                
                elif action == "typing":
                    await presence_mgr.set_typing(agent_name)
                
                elif action == "send":
                    room_id = msg.get("room_id", "general")
                    content = msg.get("content", "")
                    structured = msg.get("structured")
                    
                    mentions_spencer = "@Spencer" in content
                    requires_human = structured.get("requires_human", False) if structured else False
                    
                    msg_id = save_message(room_id, agent_name, content, structured,
                                          requires_human, mentions_spencer)
                    
                    await broadcast_to_room(room_id, {
                        "type": "message",
                        "id": msg_id,
                        "room_id": room_id,
                        "sender": agent_name,
                        "content": content,
                        "structured": structured,
                        "requires_human": requires_human,
                        "mentions_spencer": mentions_spencer,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                
                elif action == "presence":
                    status = msg.get("status", "online")
                    current_room = msg.get("current_room")
                    set_presence(agent_name, status, current_room)
                
            except json.JSONDecodeError:
                pass
    
    except WebSocketDisconnect:
        await presence_mgr.disconnect(agent_name, websocket)
    except Exception as e:
        print(f"WebSocket error for {agent_name}: {e}")
        await presence_mgr.disconnect(agent_name, websocket)


# --- Spencer Relay Endpoint (Data only) ---

@app.get("/api/v2/relay")
async def get_relay_queue(token: str):
    agent = get_agent_by_token(token)
    if not agent or agent["name"] != "Data":
        raise HTTPException(status_code=403, detail="Invalid token")
    
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """SELECT m.* FROM messages m
           WHERE (m.mentions_spencer = TRUE OR m.requires_human = TRUE)
           AND m.timestamp > datetime('now', '-24 hours')
           ORDER BY m.timestamp DESC LIMIT 50"""
    )
    rows = cursor.fetchall()
    conn.close()
    return {"pending": [dict(r) for r in rows]}


# --- File watcher for live HTML reloads ---
HTML_PATH = os.path.join(os.path.dirname(__file__), "v2_chat.html")
_html_last_mtime = os.path.getmtime(HTML_PATH)

async def _broadcast_reload():
    await presence_mgr.broadcast_all({"type": "reload", "timestamp": datetime.utcnow().isoformat()})

def _watch_html():
    global _html_last_mtime
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        time.sleep(2)
        try:
            mtime = os.path.getmtime(HTML_PATH)
            if mtime != _html_last_mtime:
                _html_last_mtime = mtime
                loop.run_until_complete(_broadcast_reload())
        except Exception:
            pass

watcher_thread = threading.Thread(target=_watch_html, daemon=True)
watcher_thread.start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
