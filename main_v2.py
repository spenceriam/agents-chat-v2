"""
Agent Chat Server v2
Structured messaging, channels, DMs, and notification inbox for AI agents.
Parallel deployment with v1 (port 8081).
"""
import base64
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import uuid
import asyncio
import httpx
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Configuration — absolute DB path anchored to script directory
BASE_DIR = Path(__file__).resolve().parent
DATABASE = os.getenv("ACV2_DATABASE", str(BASE_DIR / "chat_v2.db"))

# Environment-based configuration with sensible defaults
AGENT_TOKENS = json.loads(os.getenv("ACV2_AGENT_TOKENS", '{"spencer": "", "Data": ""}'))
MAX_HISTORY_PER_CHANNEL = int(os.getenv("ACV2_MAX_HISTORY", "50"))
PRESENCE_AWAY_MINUTES = int(os.getenv("ACV2_PRESENCE_AWAY_MINUTES", "10"))
PRESENCE_OFFLINE_SECONDS = int(os.getenv("ACV2_PRESENCE_OFFLINE_SECONDS", "60"))
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("ACV2_HEARTBEAT_TIMEOUT", "45"))  # No heartbeat = mark away
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("ACV2_HEARTBEAT_INTERVAL", "30"))  # Agents should heartbeat every 30s

# Structured logging setup
logger = logging.getLogger("acv2")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Graceful shutdown flag
_shutdown_event = threading.Event()
_background_tasks: set = set()  # Track asyncio.create_task() for cleanup


def _safe_task(coro):
    """Create a background task and track it for shutdown cleanup.
    Automatically removes the task from the set when it completes.
    Logs exceptions instead of silently swallowing them.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _cancel_background_tasks():
    """Cancel all tracked background tasks during shutdown."""
    for task in _background_tasks:
        if not task.done():
            task.cancel()

# In-memory presence and websocket tracking

@contextmanager
def get_db(row_factory: bool = True):
    """Context manager for SQLite connections. Ensures connections are always closed.
    Enables WAL mode, foreign keys, and performance PRAGMAs on each connection.
    Automatically commits on success, rolls back on exception.

    Usage:
        with get_db() as conn:
            conn.execute("SELECT ...")
        # conn automatically committed and closed

        with get_db(row_factory=False) as conn:
            conn.execute("SELECT ...")
        # Without row_factory, cursor returns tuples instead of dict-like Rows
    """
    conn = sqlite3.connect(DATABASE, timeout=10)
    try:
        # Performance and safety PRAGMAs (per-connection)
        conn.execute("PRAGMA journal_mode=WAL")       # Better concurrent read performance
        conn.execute("PRAGMA foreign_keys=ON")         # Enforce foreign key constraints
        conn.execute("PRAGMA busy_timeout=10000")      # Wait up to 10s on locked DB
        conn.execute("PRAGMA cache_size=-2000")        # 2MB page cache (negative = KB)
        conn.execute("PRAGMA synchronous=NORMAL")      # WAL mode is safe with NORMAL

        if row_factory:
            conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()  # Commit on success

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class RateLimiter:
    """Simple token-bucket rate limiter per agent."""
    def __init__(self, max_tokens: int = 10, refill_rate: float = 1.0):
        # max_tokens: max burst size, refill_rate: tokens per second
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate
        self.buckets: Dict[str, dict] = {}  # agent_name -> {"tokens": float, "last_refill": datetime}
        self.lock = asyncio.Lock()
    
    async def consume(self, agent_name: str, tokens: int = 1) -> bool:
        """Try to consume tokens. Returns True if allowed, False if rate limited."""
        async with self.lock:
            now = datetime.now(timezone.utc)
            if agent_name not in self.buckets:
                self.buckets[agent_name] = {"tokens": self.max_tokens, "last_refill": now}
            
            bucket = self.buckets[agent_name]
            elapsed = (now - bucket["last_refill"]).total_seconds()
            bucket["tokens"] = min(self.max_tokens, bucket["tokens"] + elapsed * self.refill_rate)
            bucket["last_refill"] = now
            
            if bucket["tokens"] >= tokens:
                bucket["tokens"] -= tokens
                return True
            return False
    
    async def get_status(self, agent_name: str) -> dict:
        """Get current rate limit status for an agent."""
        async with self.lock:
            now = datetime.now(timezone.utc)
            if agent_name not in self.buckets:
                return {"tokens_available": self.max_tokens, "max_tokens": self.max_tokens}
            bucket = self.buckets[agent_name]
            elapsed = (now - bucket["last_refill"]).total_seconds()
            available = min(self.max_tokens, bucket["tokens"] + elapsed * self.refill_rate)
            return {"tokens_available": round(available, 1), "max_tokens": self.max_tokens, 
                    "refill_rate": self.refill_rate}

    def purge_stale(self, idle_seconds: float = 3600):
        """Remove rate limit buckets for agents that haven't been seen in idle_seconds.
        Prevents unbounded memory growth from one-off or disconnected agents.
        """
        now = datetime.now(timezone.utc)
        stale_keys = [
            name for name, bucket in self.buckets.items()
            if (now - bucket["last_refill"]).total_seconds() > idle_seconds
        ]
        for key in stale_keys:
            del self.buckets[key]
        if stale_keys:
            logger.info(f"RateLimiter: purged {len(stale_keys)} stale agent buckets")

rate_limiter = RateLimiter(
    max_tokens=int(os.getenv("ACV2_RATE_LIMIT_BURST", "10")),
    refill_rate=float(os.getenv("ACV2_RATE_LIMIT_REFILL", "1.0"))
)


class PresenceManager:
    def __init__(self):
        self.connections: Dict[str, List[WebSocket]] = {}  # agent_name -> [websockets]
        self.subscriptions: Dict[str, Dict[str, List[str]]] = {}  # agent_name -> {websocket_id -> [room_ids]}
        self.room_subscribers: Dict[str, Dict[str, WebSocket]] = {}  # room_id -> {agent_name -> websocket} (O(1) lookup)
        self.typing: Dict[str, datetime] = {}  # agent_name -> last typing timestamp
        self.heartbeats: Dict[str, datetime] = {}  # agent_name -> last heartbeat timestamp
        self.reconnect_attempts: Dict[str, int] = {}  # agent_name -> consecutive reconnect attempts
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
        message = {"type": "presence_update", "agent": agent_name, "status": status, "timestamp": datetime.now(timezone.utc).isoformat()}
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
            self.typing[agent_name] = datetime.now(timezone.utc)
    
    async def record_heartbeat(self, agent_name: str):
        """Record agent heartbeat to confirm liveness."""
        async with self.lock:
            self.heartbeats[agent_name] = datetime.now(timezone.utc)
            # Reset reconnect attempts on heartbeat
            self.reconnect_attempts.pop(agent_name, None)
    
    def get_reconnect_delay(self, agent_name: str) -> float:
        """Calculate exponential backoff delay for reconnection (in seconds)."""
        attempts = self.reconnect_attempts.get(agent_name, 0)
        # Exponential backoff: 1s, 2s, 4s, 8s, max 60s
        return min(2 ** attempts, 60.0)
    
    async def handle_reconnect(self, agent_name: str):
        """Track reconnect attempt and apply backoff."""
        async with self.lock:
            attempts = self.reconnect_attempts.get(agent_name, 0) + 1
            self.reconnect_attempts[agent_name] = attempts
        delay = self.get_reconnect_delay(agent_name)
        return {"attempts": attempts, "delay": delay}
    
    def get_status(self, agent_name: str) -> str:
        conn_count = len(self.connections.get(agent_name, []))
        if conn_count > 0:
            last_typing = self.typing.get(agent_name)
            last_heartbeat = self.heartbeats.get(agent_name)
            last_seen = get_presence_last_seen(agent_name)
            # Check heartbeat staleness first
            if last_heartbeat and (datetime.now(timezone.utc) - last_heartbeat).total_seconds() > HEARTBEAT_TIMEOUT_SECONDS:
                return "away"
            if last_typing and (datetime.now(timezone.utc) - last_typing).total_seconds() < 120:
                return "online"
            if last_seen and (datetime.now(timezone.utc) - last_seen).total_seconds() < PRESENCE_AWAY_MINUTES * 60:
                return "online"
            return "away"
        # Check if recently disconnected (heartbeat still valid)
        last_heartbeat = self.heartbeats.get(agent_name)
        if last_heartbeat and (datetime.now(timezone.utc) - last_heartbeat).total_seconds() < HEARTBEAT_TIMEOUT_SECONDS:
            return "away"
        return "offline"

    def purge_stale(self, idle_seconds: float = 600):
        """Remove stale typing, heartbeat, and reconnect entries for agents not seen recently.
        Prevents unbounded memory growth from disconnected agents.
        """
        now = datetime.now(timezone.utc)
        stale_heartbeats = [
            name for name, ts in self.heartbeats.items()
            if (now - ts).total_seconds() > idle_seconds
        ]
        stale_typing = [
            name for name, ts in self.typing.items()
            if (now - ts).total_seconds() > idle_seconds
        ]
        stale_reconnects = [
            name for name in self.reconnect_attempts
            if name not in self.heartbeats and name not in self.connections
        ]
        for key in stale_heartbeats:
            del self.heartbeats[key]
        for key in stale_typing:
            del self.typing[key]
        for key in stale_reconnects:
            del self.reconnect_attempts[key]
        total = len(stale_heartbeats) + len(stale_typing) + len(stale_reconnects)
        if total:
            logger.info(f"PresenceManager: purged {total} stale entries (hb={len(stale_heartbeats)}, typing={len(stale_typing)}, reconnect={len(stale_reconnects)})")

presence_mgr = PresenceManager()


# --- API Metrics (IMPROVE-011) ---
class APIMetrics:
    """In-memory request metrics for monitoring ACV2 health."""
    def __init__(self):
        self.request_count = 0
        self.error_count = 0
        self.broadcast_count = 0
        self.broadcast_total_latency_ms = 0.0
        self.message_count = 0
        self.ws_connections = 0
        self.start_time = datetime.now(timezone.utc)
        self.lock = threading.Lock()

    def record_request(self):
        with self.lock:
            self.request_count += 1

    def record_error(self):
        with self.lock:
            self.error_count += 1

    def record_broadcast(self, subscriber_count: int, latency_ms: float):
        with self.lock:
            self.broadcast_count += 1
            self.broadcast_total_latency_ms += latency_ms

    def record_message(self):
        with self.lock:
            self.message_count += 1

    def record_ws_connect(self):
        with self.lock:
            self.ws_connections += 1

    def record_ws_disconnect(self):
        with self.lock:
            self.ws_connections = max(0, self.ws_connections - 1)

    def get_stats(self) -> dict:
        with self.lock:
            uptime = (datetime.now(timezone.utc) - self.start_time).total_seconds()
            avg_broadcast_ms = (
                self.broadcast_total_latency_ms / self.broadcast_count
                if self.broadcast_count > 0 else 0
            )
            return {
                "uptime_seconds": round(uptime, 1),
                "total_requests": self.request_count,
                "total_errors": self.error_count,
                "error_rate": round(self.error_count / max(1, self.request_count) * 100, 2),
                "messages_sent": self.message_count,
                "broadcasts": self.broadcast_count,
                "avg_broadcast_latency_ms": round(avg_broadcast_ms, 2),
                "active_ws_connections": self.ws_connections,
            }


metrics = APIMetrics()


def init_db():
    """Initialize database schema, indexes, triggers, and seed data."""
    with get_db(row_factory=False) as conn:
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
                image_url TEXT,
                image_type TEXT,
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

        # Enhanced presence columns
        try:
            cursor.execute("ALTER TABLE presence ADD COLUMN status_detail TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE presence ADD COLUMN status_message TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        # FTS5 message search
        cursor.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content, sender, room_id,
                content='messages', content_rowid='rowid',
                tokenize='porter unicode61'
            )
        ''')

        # Triggers to keep FTS index in sync
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, sender, room_id)
                VALUES (NEW.rowid, NEW.content, NEW.sender, NEW.room_id);
            END
        ''')
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, sender, room_id)
                VALUES ('delete', OLD.rowid, OLD.content, OLD.sender, OLD.room_id);
            END
        ''')
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, sender, room_id)
                VALUES ('delete', OLD.rowid, OLD.content, OLD.sender, OLD.room_id);
                INSERT INTO messages_fts(rowid, content, sender, room_id)
                VALUES (NEW.rowid, NEW.content, NEW.sender, NEW.room_id);
            END
        ''')

        # Thread/subchannel tables (FEAT-001 Phase 1)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                parent_channel_id TEXT REFERENCES rooms(id),
                creator TEXT,
                topic TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_archived BOOLEAN DEFAULT FALSE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS thread_members (
                thread_id TEXT REFERENCES threads(id),
                agent_name TEXT,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (thread_id, agent_name)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_threads_parent ON threads(parent_channel_id, is_archived)
        ''')

        # Migration: add thread_id to messages if missing
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN thread_id TEXT REFERENCES threads(id)")
        except sqlite3.OperationalError:
            pass

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

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_ack (
                message_id TEXT REFERENCES messages(id),
                agent_name TEXT,
                acked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, agent_name)
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
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN image_url TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN image_type TEXT")
        except sqlite3.OperationalError:
            pass

        # Message reactions (UX-002)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_reactions (
                message_id TEXT REFERENCES messages(id),
                agent_name TEXT,
                emoji TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, agent_name, emoji)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_reactions_message ON message_reactions(message_id)
        ''')

        # Pinned messages (UX-004)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pinned_messages (
                id TEXT PRIMARY KEY,
                room_id TEXT REFERENCES rooms(id),
                message_id TEXT REFERENCES messages(id),
                pinned_by TEXT,
                content_preview TEXT,
                pinned_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_pinned_room ON pinned_messages(room_id)
        ''')

        conn.commit()


def load_agents():
    global AGENT_TOKENS
    try:
        with open("agents.json", "r") as f:
            AGENT_TOKENS = json.load(f)
    except FileNotFoundError:
        AGENT_TOKENS = {}


def get_agent_by_token(token: str) -> Optional[dict]:
    return AGENT_TOKENS.get(token)


def set_presence(agent_name: str, status: str, current_room: str = None, status_detail: str = None, status_message: str = None):
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        # Build dynamic update for optional fields
        updates = ["status=excluded.status", "last_seen=excluded.last_seen", "current_room=excluded.current_room"]
        values = [agent_name, status, datetime.now(timezone.utc).isoformat(), current_room]
        
        if status_detail is not None:
            updates.append("status_detail=excluded.status_detail")
            values.append(status_detail)
        
        if status_message is not None:
            updates.append("status_message=excluded.status_message")
            values.append(status_message)
        
        placeholders = ", ".join(["?"] * len(values))
        cols = "agent_name, status, last_seen, current_room"
        if status_detail is not None:
            cols += ", status_detail"
        if status_message is not None:
            cols += ", status_message"
        
        cursor.execute(
            f"""INSERT INTO presence ({cols})
               VALUES ({placeholders})
               ON CONFLICT(agent_name) DO UPDATE SET
               {", ".join(updates)}""",
            values
        )


def get_presence_last_seen(agent_name: str) -> Optional[datetime]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_seen FROM presence WHERE agent_name = ?", (agent_name,))
        row = cursor.fetchone()
    if row:
        return datetime.fromisoformat(row["last_seen"])
    return None


def get_all_presence() -> List[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT agent_name, status, last_seen, current_room, status_detail, status_message FROM presence")
        rows = cursor.fetchall()
    return [dict(r) for r in rows]


def mark_room_read(agent_name: str, room_id: str):
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO room_read_receipts (agent_name, room_id, last_read_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(agent_name, room_id) DO UPDATE SET
               last_read_at = excluded.last_read_at""",
            (agent_name, room_id)
        )


def record_message_ack(message_id: str, agent_name: str):
    """Record that an agent has acknowledged receiving a message."""
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO message_ack (message_id, agent_name, acked_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(message_id, agent_name) DO UPDATE SET
               acked_at = excluded.acked_at""",
            (message_id, agent_name)
        )

def get_message_ack_status(message_id: str) -> dict:
    """Get acknowledgment status for a message: who has acked it."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT agent_name, acked_at FROM message_ack WHERE message_id = ? ORDER BY acked_at",
            (message_id,)
        )
        acks = [dict(r) for r in cursor.fetchall()]
        cursor.execute(
            "SELECT rm.agent_name FROM room_members rm JOIN messages m ON m.room_id = rm.room_id WHERE m.id = ? AND rm.agent_name != m.sender",
            (message_id,)
        )
        expected = [r["agent_name"] for r in cursor.fetchall()]

        acked_names = [a["agent_name"] for a in acks]
        return {
            "message_id": message_id,
            "expected": expected,
            "acked": acks,
            "pending": [e for e in expected if e not in acked_names],
            "all_acked": len([e for e in expected if e not in acked_names]) == 0
        }


def get_unacked_messages(agent_name: str, limit: int = 20) -> list:
    """Get messages sent by an agent that haven't been fully acknowledged."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT m.id, m.room_id, m.sender, m.content, m.timestamp,
                      (SELECT COUNT(*) FROM room_members rm2 JOIN messages m2 ON m2.room_id = rm2.room_id
                       WHERE m2.id = m.id AND rm2.agent_name != m.sender) as expected_count,
                      (SELECT COUNT(*) FROM message_ack ma WHERE ma.message_id = m.id) as ack_count
               FROM messages m
               WHERE m.sender = ?
               AND m.timestamp > datetime('now', '-1 hour')
               AND m.is_deleted = FALSE
               AND (SELECT COUNT(*) FROM message_ack ma WHERE ma.message_id = m.id) <
                   (SELECT COUNT(*) FROM room_members rm2 JOIN messages m2 ON m2.room_id = rm2.room_id
                    WHERE m2.id = m.id AND rm2.agent_name != m.sender)
               ORDER BY m.timestamp DESC LIMIT ?""",
            (agent_name, limit)
        )
        results = [dict(r) for r in cursor.fetchall()]

        return results


# --- Message Reactions (UX-002) ---
ALLOWED_EMOJIS = {"👍", "👎", "❤️", "😂", "🎉", "🚀", "👀", "💯", "✅", "🔥", "⭐", "👏", "💡", "🤔", "😅", "🎯"}

def add_reaction(message_id: str, agent_name: str, emoji: str) -> dict:
    """Add a reaction to a message. Returns reaction state."""
    if emoji not in ALLOWED_EMOJIS:
        return {"error": f"Emoji not in allowed set: {emoji}"}
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        # Check message exists
        cursor.execute("SELECT room_id FROM messages WHERE id = ?", (message_id,))
        row = cursor.fetchone()
        if not row:
    
            return {"error": "Message not found"}
        room_id = row[0]
        cursor.execute(
            "INSERT INTO message_reactions (message_id, agent_name, emoji) VALUES (?, ?, ?)\n           ON CONFLICT(message_id, agent_name, emoji) DO NOTHING",
            (message_id, agent_name, emoji)
        )
    
        # Get updated reactions for this message
        cursor.execute(
            "SELECT emoji, COUNT(*) as count, GROUP_CONCAT(agent_name) as agents FROM message_reactions WHERE message_id = ? GROUP BY emoji ORDER BY count DESC",
            (message_id,)
        )
        reactions = [{"emoji": r[0], "count": r[1], "agents": r[2].split(",")} for r in cursor.fetchall()]
    
        return {"message_id": message_id, "room_id": room_id, "reactions": reactions}
    
def remove_reaction(message_id: str, agent_name: str, emoji: str) -> dict:
    """Remove a reaction from a message."""
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT room_id FROM messages WHERE id = ?", (message_id,))
        row = cursor.fetchone()
        if not row:

            return {"error": "Message not found"}
        room_id = row[0]
        cursor.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND agent_name = ? AND emoji = ?",
            (message_id, agent_name, emoji)
        )

        cursor.execute(
            "SELECT emoji, COUNT(*) as count, GROUP_CONCAT(agent_name) as agents FROM message_reactions WHERE message_id = ? GROUP BY emoji ORDER BY count DESC",
            (message_id,)
        )
        reactions = [{"emoji": r[0], "count": r[1], "agents": r[2].split(",")} for r in cursor.fetchall()]

        return {"message_id": message_id, "room_id": room_id, "reactions": reactions}

def get_message_reactions(message_id: str) -> list:
    """Get all reactions for a message."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT emoji, COUNT(*) as count, GROUP_CONCAT(agent_name) as agents FROM message_reactions WHERE message_id = ? GROUP BY emoji ORDER BY count DESC",
            (message_id,)
        )
        reactions = [{"emoji": r["emoji"], "count": r["count"], "agents": r["agents"].split(",")} for r in cursor.fetchall()]

        return reactions

def get_room_reactions(room_id: str, limit: int = 50) -> dict:
    """Get reactions for recent messages in a room. Returns {message_id: [reactions]}."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM messages WHERE room_id = ? ORDER BY timestamp DESC LIMIT ?",
            (room_id, limit)
        )
        msg_ids = [r["id"] for r in cursor.fetchall()]
        result = {}
        for mid in msg_ids:
            cursor.execute(
                "SELECT emoji, COUNT(*) as count, GROUP_CONCAT(agent_name) as agents FROM message_reactions WHERE message_id = ? GROUP BY emoji",
                (mid,)
            )
            reactions = [{"emoji": r["emoji"], "count": r["count"], "agents": r["agents"].split(",")} for r in cursor.fetchall()]
            if reactions:
                result[mid] = reactions

        return result

# --- Message Pinning (UX-004) ---
def pin_message(message_id: str, room_id: str, pinned_by: str) -> dict:
    """Pin a message to the top of a room."""
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT content FROM messages WHERE id = ? AND room_id = ?", (message_id, room_id))
        row = cursor.fetchone()
        if not row:

            return {"error": "Message not found in this room"}
        content_preview = row[0][:200] if row[0] else ""
        pin_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT OR REPLACE INTO pinned_messages (id, room_id, message_id, pinned_by, content_preview) VALUES (?, ?, ?, ?, ?)",
            (pin_id, room_id, message_id, pinned_by, content_preview)
        )

        return {"id": pin_id, "room_id": room_id, "message_id": message_id, "pinned_by": pinned_by, "content_preview": content_preview}

def unpin_message(room_id: str, message_id: str) -> bool:
    """Unpin a message from a room."""
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pinned_messages WHERE room_id = ? AND message_id = ?", (room_id, message_id))

        deleted = cursor.rowcount > 0

        return deleted

def get_pinned_messages(room_id: str) -> list:
    """Get all pinned messages for a room."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, message_id, pinned_by, content_preview, pinned_at FROM pinned_messages WHERE room_id = ? ORDER BY pinned_at DESC",
            (room_id,)
        )
        pins = [dict(r) for r in cursor.fetchall()]

        return pins


def search_messages(query: str, room_id: str = None, sender: str = None, limit: int = 50) -> list:
    """Full-text search across messages using FTS5."""
    with get_db() as conn:
        cursor = conn.cursor()
    
        # Build FTS query with optional filters
        fts_conditions = []
        params = []
    
        if room_id:
            fts_conditions.append("room_id = ?")
            params.append(room_id)
        if sender:
            fts_conditions.append("sender = ?")
            params.append(sender)
    
        where_clause = " AND ".join(fts_conditions) if fts_conditions else "1=1"
        params.append(query)
        params.append(limit)
    
        # FTS5 search with bm25 ranking
        cursor.execute(
            f"""SELECT m.id, m.room_id, m.sender, m.content, m.structured, m.timestamp, m.thread_id,
                       m.is_deleted, m.edited_at,
                       rank
                FROM messages_fts f
                JOIN messages m ON m.rowid = f.rowid
                WHERE messages_fts MATCH ? AND {where_clause} AND m.is_deleted = FALSE
                ORDER BY rank
                LIMIT ?""",
            params
        )
        results = []
        for row in cursor.fetchall():
            msg = dict(row)
            if msg.get("structured"):
                msg["structured"] = json.loads(msg["structured"])
            results.append(msg)
    
        return results
    
    
def get_unread_counts(agent_name: str) -> Dict[str, int]:
    with get_db(row_factory=False) as conn:
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

        return {r[0]: r[1] for r in rows if r[1] > 0}


def get_banner() -> str:
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", ("banner_text",))
        row = cursor.fetchone()

        return row[0] if row else ""


def set_banner(text: str):
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("banner_text", text)
        )

def create_dm(agent_a: str, agent_b: str) -> str:
    if agent_a == agent_b:
        raise ValueError("Cannot create a DM with yourself")
    if agent_a.startswith("dm_") or agent_b.startswith("dm_"):
        raise ValueError("Agent names must not be room IDs")
    agents_sorted = sorted([agent_a, agent_b])
    room_id = f"dm_{agents_sorted[0]}_{agents_sorted[1]}"
    
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO rooms (id, type, name, created_by, metadata) VALUES (?, 'dm', ?, 'system', ?)",
            (room_id, f"DM: {agent_a} & {agent_b}", json.dumps({"participants": [agent_a, agent_b]}))
        )
        cursor.execute(
            "INSERT OR IGNORE INTO room_members (room_id, agent_name) VALUES (?, ?), (?, ?)",
            (room_id, agent_a, room_id, agent_b)
        )

        return room_id


def get_or_create_room(room_id: str, room_type: str = "channel") -> dict:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rooms WHERE id = ?", (room_id,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None


# --- Thread helpers (FEAT-001 Phase 1) ---

def create_thread(channel_id: str, creator: str, topic: str) -> dict:
    thread_id = f"thread_{channel_id}_{uuid.uuid4().hex[:8]}"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM rooms WHERE id = ? AND type = 'channel'", (channel_id,))
        if not cursor.fetchone():

            raise ValueError(f"Channel '{channel_id}' not found")
        cursor.execute(
            "INSERT INTO threads (id, parent_channel_id, creator, topic) VALUES (?, ?, ?, ?)",
            (thread_id, channel_id, creator, topic)
        )
        cursor.execute("INSERT INTO thread_members (thread_id, agent_name) VALUES (?, ?)", (thread_id, creator))

        cursor.execute("SELECT * FROM threads WHERE id = ?", (thread_id,))
        thread = dict(cursor.fetchone())

        return thread


def list_threads(channel_id: str, include_archived: bool = False) -> list:
    with get_db() as conn:
        cursor = conn.cursor()
        af = "" if include_archived else "AND t.is_archived = FALSE"
        cursor.execute(
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM thread_members tm WHERE tm.thread_id = t.id) as member_count, "
            "(SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) as message_count, "
            "(SELECT MAX(timestamp) FROM messages m WHERE m.thread_id = t.id) as last_message_at "
            "FROM threads t WHERE t.parent_channel_id = ? " + af +
            " ORDER BY t.created_at DESC",
            (channel_id,)
        )
        threads = [dict(r) for r in cursor.fetchall()]
    
        return threads
    
    
def get_thread(thread_id: str) -> dict | None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM threads WHERE id = ?", (thread_id,))
        thread = cursor.fetchone()
        if not thread:

            return None
        thread = dict(thread)
        cursor.execute("SELECT agent_name, joined_at FROM thread_members WHERE thread_id = ? ORDER BY joined_at", (thread_id,))
        thread["members"] = [dict(r) for r in cursor.fetchall()]

        return thread


def join_thread(thread_id: str, agent_name: str) -> bool:
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO thread_members (thread_id, agent_name) VALUES (?, ?)", (thread_id, agent_name))
    
            return True
        except Exception:
    
            return False
    
    
def archive_thread(thread_id: str) -> bool:
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE threads SET is_archived = TRUE WHERE id = ?", (thread_id,))
        updated = cursor.rowcount > 0

        return updated


def update_thread_topic(thread_id: str, topic: str) -> bool:
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE threads SET topic = ? WHERE id = ?", (topic, thread_id))
        updated = cursor.rowcount > 0

        return updated


def get_thread_messages(thread_id: str, limit: int = 50, before_id: str = None) -> list:
    with get_db() as conn:
        cursor = conn.cursor()
        if before_id:
            cursor.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND timestamp < "
                "(SELECT timestamp FROM messages WHERE id = ?) ORDER BY timestamp DESC LIMIT ?",
                (thread_id, before_id, limit)
            )
        else:
            cursor.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY timestamp DESC LIMIT ?",
                (thread_id, limit)
            )
        rows = cursor.fetchall()
    
        messages = []
        for row in reversed(rows):
            msg = dict(row)
            if msg.get("structured"):
                msg["structured"] = json.loads(msg["structured"])
            messages.append(msg)
        return messages
    
    
def save_thread_message(thread_id: str, room_id: str, sender: str, content: str,
                        structured: dict = None, requires_human: bool = False,
                        mentions_spencer: bool = False,
                        image_url: str = None, image_type: str = None) -> str:
    msg_id = str(uuid.uuid4())
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (id, room_id, thread_id, sender, content, structured, requires_human, mentions_spencer, is_deleted, image_url, image_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?, ?)",
            (msg_id, room_id, thread_id, sender, content,
             json.dumps(structured) if structured else None,
             requires_human, mentions_spencer, image_url, image_type)
        )

        # Create notifications for message
        create_notifications_for_message(msg_id, room_id, sender, content, mentions_spencer)
        metrics.record_message()
        return msg_id


def save_message(room_id: str, sender: str, content: str, structured: dict = None,
                 requires_human: bool = False, mentions_spencer: bool = False,
                 image_url: str = None, image_type: str = None) -> str:
    msg_id = str(uuid.uuid4())
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO messages (id, room_id, sender, content, structured, requires_human, mentions_spencer, is_deleted, image_url, image_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, FALSE, ?, ?)""",
            (msg_id, room_id, sender, content,
             json.dumps(structured) if structured else None,
             requires_human, mentions_spencer, image_url, image_type)
        )

        # Create notifications for message
        create_notifications_for_message(msg_id, room_id, sender, content, mentions_spencer, conn)
        metrics.record_message()
        return msg_id


def create_notifications_for_message(message_id: str, room_id: str, sender: str, content: str, mentions_spencer: bool, conn: sqlite3.Connection = None):
    """Create notification records for a new message.
    If conn is provided (caller is already in a transaction), use it.
    Otherwise opens its own connection."""
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(DATABASE, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
    cursor = conn.cursor()

    try:
        # Get room info
        cursor.execute("SELECT type, name FROM rooms WHERE id = ?", (room_id,))
        room = cursor.fetchone()
        if not room:
            return

        # Handle both dict-like rows and tuples
        room_type = room[0]
        room_name = room[1]

        # For DMs, notify the other participant
        if room_type == "dm":
            cursor.execute("SELECT agent_name FROM room_members WHERE room_id = ? AND agent_name != ?", (room_id, sender))
            for row in cursor.fetchall():
                recipient = row[0]
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
    finally:
        if own_conn:
            try:
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
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
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            except Exception:
                pass
    # Channel mentions
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT type FROM rooms WHERE id = ?", (room_id,))
        row = cursor.fetchone()
    room_type = row[0] if row else None

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
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                except Exception:
                    pass


def get_room_messages(room_id: str, limit: int = 50, before_id: str = None) -> List[dict]:
    with get_db() as conn:
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
        messages = []
        for row in reversed(rows):
            msg = dict(row)
            if msg.get("structured"):
                msg["structured"] = json.loads(msg["structured"])
            msg["reactions"] = []
            messages.append(msg)
    
        # Batch load reactions for all messages in a single query (fixes N+1)
        if messages:
            msg_ids = [m["id"] for m in messages]
            placeholders = ",".join("?" * len(msg_ids))
            cursor.execute(
                f"SELECT message_id, emoji, COUNT(*) as count, GROUP_CONCAT(agent_name) as agents "
                f"FROM message_reactions WHERE message_id IN ({placeholders}) "
                f"GROUP BY message_id, emoji ORDER BY count DESC",
                msg_ids
            )
            reactions_by_msg: Dict[str, list] = {}
            for r in cursor.fetchall():
                reactions_by_msg.setdefault(r["message_id"], []).append(
                    {"emoji": r["emoji"], "count": r["count"], "agents": r["agents"].split(",")}
                )
            for msg in messages:
                msg["reactions"] = reactions_by_msg.get(msg["id"], [])
    
        return messages
    
    
def get_notifications(agent_name: str, cleared: bool = False) -> List[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM notifications 
               WHERE agent_name = ? AND cleared = ?
               ORDER BY created_at DESC LIMIT 50""",
            (agent_name, cleared)
        )
        rows = cursor.fetchall()

        return [dict(r) for r in rows]


def clear_notifications(agent_name: str, notification_ids: List[str] = None):
    with get_db(row_factory=False) as conn:
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

def ensure_agent_in_default_channels(agent_name: str):
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        for room_id in ["general", "agenttracker", "ops"]:
            cursor.execute(
                "INSERT OR IGNORE INTO room_members (room_id, agent_name) VALUES (?, ?)",
                (room_id, agent_name)
            )

def get_agent_rooms(agent_name: str) -> List[dict]:
    ensure_agent_in_default_channels(agent_name)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT r.* FROM rooms r
               JOIN room_members rm ON r.id = rm.room_id
               WHERE rm.agent_name = ?
               ORDER BY r.type, r.name""",
            (agent_name,)
        )
        rows = cursor.fetchall()

        return [dict(r) for r in rows]


def get_dm_room_id(agent_a: str, agent_b: str) -> Optional[str]:
    agents_sorted = sorted([agent_a, agent_b])
    room_id = f"dm_{agents_sorted[0]}_{agents_sorted[1]}"
    room = get_or_create_room(room_id)
    return room_id if room else None


# Pydantic models
class SendMessageRequest(BaseModel):
    """Send a message to a channel, DM, or thread."""
    content: str = Field(default="", description="Message text (supports Markdown)")
    structured: Optional[dict] = Field(default=None, description="Structured data payload")
    room_id: str = Field(default="general", description="Target room (channel, DM, or thread ID)")
    image_url: Optional[str] = Field(default=None, description="Screenshot/image reference (file path or URL)")
    image_type: Optional[str] = Field(default=None, description="MIME type: image/png, image/jpeg")


class CreateChannelRequest(BaseModel):
    """Create a new channel room."""
    id: str = Field(description="Channel identifier (e.g., 'engineering')")
    name: str = Field(description="Display name")
    metadata: Optional[dict] = Field(default=None, description="Optional channel metadata")


class SubscribeRequest(BaseModel):
    """Subscribe to rooms and set presence status."""
    rooms: List[str] = Field(description="List of room IDs to subscribe to")
    presence: str = Field(default="online", description="Presence status: online, away, busy")


class WebhookRegisterRequest(BaseModel):
    """Register a webhook for event notifications."""
    url: str = Field(description="Webhook callback URL")
    events: List[str] = Field(default=["all"], description="Event types to subscribe to")


class CreateThreadRequest(BaseModel):
    """Create a new thread within a room."""
    topic: str = Field(description="Thread topic/title")
    metadata: Optional[dict] = Field(default=None, description="Optional thread metadata")


class SendThreadMessageRequest(BaseModel):
    """Send a message within a thread."""
    content: str = Field(default="", description="Message text (supports Markdown)")
    structured: Optional[dict] = Field(default=None, description="Structured data payload")
    image_url: Optional[str] = Field(default=None, description="Image attachment URL or path")
    image_type: Optional[str] = Field(default=None, description="MIME type of image")


class ThreadUpdateRequest(BaseModel):
    """Update thread properties."""
    topic: str = Field(description="New thread topic/title")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: init DB, load config, handle graceful shutdown."""
    init_db()
    load_agents()
    logger.info("ACV2 server started")
    try:
        yield
    finally:
        logger.info("ACV2 server shutting down, closing WebSocket connections...")
        _shutdown_event.set()
        _cancel_background_tasks()
        # Close all active WebSocket connections gracefully
        async with presence_mgr.lock:
            for agent_name, ws_list in list(presence_mgr.connections.items()):
                for ws in ws_list:
                    try:
                        await ws.close(code=1001, reason="Server shutting down")
                    except Exception:
                        pass
                ws_list.clear()
            presence_mgr.connections.clear()
        logger.info("ACV2 server shutdown complete")


app = FastAPI(
    title="Agent Chat v2",
    description="Real-time messaging platform for AI agents and humans. Supports channels, DMs, threads, reactions, pinning, presence, and full WebSocket pub/sub.",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# Middleware: request counting, correlation IDs, and rate limit headers
@app.middleware("http")
async def count_requests(request: Request, call_next):
    metrics.record_request()
    
    # Generate or propagate correlation ID for request tracing
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    
    # Check rate limit before processing
    agent_header = request.headers.get("X-Agent-Token")
    if agent_header:
        agent = get_agent_by_token(agent_header)
        if agent:
            allowed = await rate_limiter.consume(agent["name"])
            if not allowed:
                metrics.record_error()
                from fastapi.responses import JSONResponse
                response = JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again later."}
                )
                response.headers["X-Request-ID"] = request_id
                response.headers["X-RateLimit-Limit"] = "10"
                response.headers["X-RateLimit-Remaining"] = "0"
                return response
    
    try:
        response = await call_next(request)
        # Add correlation ID to response
        response.headers["X-Request-ID"] = request_id
        
        # Add rate limit status if agent token present
        if agent_header:
            agent = get_agent_by_token(agent_header)
            if agent:
                status = await rate_limiter.get_status(agent["name"])
                response.headers["X-RateLimit-Limit"] = str(int(status.get("max_tokens", 10)))
                response.headers["X-RateLimit-Remaining"] = str(int(status.get("tokens_available", 0)))
        
        return response
    except Exception:
        metrics.record_error()
        raise


# Serve v2 chat UI
@app.get("/v2")
async def v2_ui():
    return FileResponse("v2_chat.html")

# --- API Endpoints ---

@app.get("/api/v2/health")
async def health_check():
    # Quick DB health check
    db_ok = False
    try:
        with get_db(row_factory=False) as conn:
            conn.execute("SELECT 1")
            db_ok = True
    except Exception:
        pass
    
    return {
        "status": "ok" if db_ok else "degraded",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": "connected" if db_ok else "error",
        "agents_loaded": len(AGENT_TOKENS),
        "active_connections": sum(len(ws) for ws in presence_mgr.connections.values()),
    }


@app.get("/api/v2/me")
async def get_me(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    return {"name": agent["name"], "metadata": agent.get("metadata", {})}


# --- Screenshot/Image Upload ---
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

class UploadImageRequest(BaseModel):
    """Upload an image (screenshot, attachment) as base64 data."""
    filename: str = Field(description="Original filename")
    data: str = Field(description="Base64-encoded image data")
    content_type: str = Field(description="MIME type (e.g., image/png)")


# --- Reactions & Pinning Models (UX-002, UX-004) ---
class ReactionRequest(BaseModel):
    """Add or remove a reaction emoji on a message."""
    emoji: str = Field(description="Emoji character (e.g., '👍', '🎉')")


class PinRequest(BaseModel):
    """Pin or unpin a message."""
    message_id: str = Field(description="Message ID to pin/unpin")


@app.post("/api/v2/screenshots")
async def upload_screenshot(token: str, req: UploadImageRequest):
    """Upload a screenshot/image. Returns the URL for use in message image_url field."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    # Validate content type
    if req.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type: {req.content_type}. Allowed: {', '.join(sorted(ALLOWED_IMAGE_TYPES))}"
        )
    
    # Decode and validate size
    try:
        image_bytes = base64.b64decode(req.data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 data")
    
    if len(image_bytes) > MAX_IMAGE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Image too large: {len(image_bytes)} bytes. Max: {MAX_IMAGE_SIZE} bytes (5MB)"
        )
    
    # Sanitize filename
    safe_name = "".join(c for c in req.filename if c.isalnum() or c in "._-")
    if not safe_name:
        safe_name = "screenshot"
    ext = ".png" if req.content_type == "image/png" else ".jpg" if req.content_type == "image/jpeg" else ".gif" if req.content_type == "image/gif" else ".webp"
    
    # Generate unique filename
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    unique_name = f"{agent['name']}_{timestamp}_{safe_name}{ext}"
    screenshots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    file_path = os.path.join(screenshots_dir, unique_name)
    
    with open(file_path, "wb") as f:
        f.write(image_bytes)
    
    # Return the URL path (served via static route)
    image_url = f"/screenshots/{unique_name}"
    return {"status": "uploaded", "url": image_url, "size": len(image_bytes), "content_type": req.content_type}


@app.get("/screenshots/{filename}")
async def serve_screenshot(filename: str):
    """Serve uploaded screenshots."""
    # Security: prevent directory traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    screenshots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
    file_path = os.path.join(screenshots_dir, filename)
    
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    
    # Determine content type from extension
    ext = os.path.splitext(filename)[1].lower()
    content_type_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    content_type = content_type_map.get(ext, "application/octet-stream")
    
    return FileResponse(file_path, media_type=content_type)


# --- Reaction Endpoints (UX-002) ---
@app.post("/api/v2/messages/{msg_id}/reactions")
async def add_reaction_endpoint(msg_id: str, token: str, req: ReactionRequest):
    """Add a reaction emoji to a message."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    result = add_reaction(msg_id, agent["name"], req.emoji)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    # Broadcast reaction update to room
    _safe_task(broadcast_to_room(result["room_id"], {
        "type": "reaction",
        "event": "added",
        "message_id": msg_id,
        "agent_name": agent["name"],
        "emoji": req.emoji,
        "reactions": result["reactions"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }))
    return result

@app.delete("/api/v2/messages/{msg_id}/reactions/{emoji}")
async def remove_reaction_endpoint(msg_id: str, emoji: str, token: str):
    """Remove a reaction emoji from a message."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    result = remove_reaction(msg_id, agent["name"], emoji)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    _safe_task(broadcast_to_room(result["room_id"], {
        "type": "reaction",
        "event": "removed",
        "message_id": msg_id,
        "agent_name": agent["name"],
        "emoji": emoji,
        "reactions": result["reactions"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }))
    return result

@app.get("/api/v2/messages/{msg_id}/reactions")
async def get_reactions_endpoint(msg_id: str, token: str):
    """Get all reactions for a message."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    return {"message_id": msg_id, "reactions": get_message_reactions(msg_id)}

@app.get("/api/v2/rooms/{room_id}/pinned")
async def get_pinned_endpoint(room_id: str, token: str):
    """Get all pinned messages for a room."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    return {"room_id": room_id, "pinned": get_pinned_messages(room_id)}

# --- Pinning Endpoints (UX-004) ---
@app.post("/api/v2/rooms/{room_id}/pin")
async def pin_endpoint(room_id: str, token: str, req: PinRequest):
    """Pin a message in a room."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    result = pin_message(req.message_id, room_id, agent["name"])
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    _safe_task(broadcast_to_room(room_id, {
        "type": "pin",
        "event": "added",
        "room_id": room_id,
        "message_id": req.message_id,
        "pinned_by": agent["name"],
        "pin": result,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }))
    return result

@app.delete("/api/v2/rooms/{room_id}/pin/{message_id}")
async def unpin_endpoint(room_id: str, message_id: str, token: str):
    """Unpin a message from a room."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    result = unpin_message(room_id, message_id)
    if not result:
        raise HTTPException(status_code=404, detail="Pin not found")
    _safe_task(broadcast_to_room(room_id, {
        "type": "pin",
        "event": "removed",
        "room_id": room_id,
        "message_id": message_id,
        "unpinned_by": agent["name"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }))
    return {"status": "unpinned", "room_id": room_id, "message_id": message_id}


def edit_message(msg_id: str, content: str) -> bool:
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE messages SET content = ?, edited_at = CURRENT_TIMESTAMP WHERE id = ? AND is_deleted = FALSE",
            (content, msg_id)
        )
        updated = cursor.rowcount > 0

        return updated


def delete_message(msg_id: str) -> bool:
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE messages SET content = '', is_deleted = TRUE, edited_at = CURRENT_TIMESTAMP WHERE id = ?",
            (msg_id,)
        )
        updated = cursor.rowcount > 0

        return updated


def get_message_sender(msg_id: str) -> str | None:
    """Get the sender of a message, or None if not found."""
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT sender FROM messages WHERE id = ?", (msg_id,))
        row = cursor.fetchone()

        return row[0] if row else None

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
    
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO rooms (id, type, name, created_by, metadata) VALUES (?, 'channel', ?, ?, ?)",
            (req.id, req.name, agent["name"], json.dumps(req.metadata or {}))
        )
        cursor.execute(
            "INSERT INTO room_members (room_id, agent_name) VALUES (?, ?)",
            (req.id, agent["name"])
        )

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
    # Verify message ownership (only sender or admins can edit)
    sender = get_message_sender(msg_id)
    if sender and sender != agent["name"] and agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only the message author or an admin can edit")
    if edit_message(msg_id, req.content):
        mentions_spencer = "@Spencer" in req.content
        create_notifications_for_message(msg_id, room_id, agent["name"], req.content, mentions_spencer)
        await notify_mentions_ws(room_id, agent["name"], req.content, msg_id)
        await broadcast_to_room(room_id, {
            "type": "message", "edit": True, "id": msg_id,
            "room_id": room_id, "sender": agent["name"], "content": req.content,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return {"status": "edited"}
    raise HTTPException(status_code=404, detail="Message not found or already deleted")


@app.delete("/api/v2/channels/{room_id}/messages/{msg_id}")
async def delete_channel_message(room_id: str, msg_id: str, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    # Verify message ownership
    sender = get_message_sender(msg_id)
    if sender and sender != agent["name"] and agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only the message author or an admin can delete")
    if delete_message(msg_id):
        await broadcast_to_room(room_id, {
            "type": "message", "delete": True, "id": msg_id,
            "room_id": room_id, "sender": agent["name"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Message not found")


# --- Thread API Endpoints (FEAT-001 Phase 1) ---

@app.post("/api/v2/channels/{channel_id}/threads")
async def create_thread_endpoint(channel_id: str, token: str, req: CreateThreadRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    try:
        thread = create_thread(channel_id, agent["name"], req.topic)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await broadcast_to_room(channel_id, {
        "type": "thread_created",
        "thread": thread,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
    return {"status": "created", "thread": thread}


@app.get("/api/v2/channels/{channel_id}/threads")
async def list_channel_threads(channel_id: str, token: str, archived: bool = False):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    threads = list_threads(channel_id, include_archived=archived)
    return {"channel_id": channel_id, "threads": threads}


@app.get("/api/v2/threads/{thread_id}")
async def get_thread_info(thread_id: str, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"thread": thread}


@app.put("/api/v2/threads/{thread_id}")
async def update_thread(thread_id: str, token: str, req: ThreadUpdateRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread["creator"] != agent["name"] and agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only creator or admin can update thread")
    if update_thread_topic(thread_id, req.topic):
        return {"status": "updated", "topic": req.topic}
    raise HTTPException(status_code=500, detail="Failed to update thread")


@app.post("/api/v2/threads/{thread_id}/join")
async def join_thread_endpoint(thread_id: str, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread.get("is_archived"):
        raise HTTPException(status_code=400, detail="Cannot join archived thread")
    if join_thread(thread_id, agent["name"]):
        return {"status": "joined"}
    raise HTTPException(status_code=400, detail="Already a member or failed to join")


@app.post("/api/v2/threads/{thread_id}/archive")
async def archive_thread_endpoint(thread_id: str, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread["creator"] != agent["name"] and agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only creator or admin can archive thread")
    if archive_thread(thread_id):
        await broadcast_to_room(thread["parent_channel_id"], {
            "type": "thread_archived",
            "thread_id": thread_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return {"status": "archived"}
    raise HTTPException(status_code=404, detail="Thread not found")


@app.post("/api/v2/threads/{thread_id}/messages")
async def send_thread_message(thread_id: str, token: str, req: SendThreadMessageRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    if not await rate_limiter.consume(agent["name"]):
        status = await rate_limiter.get_status(agent["name"])
        raise HTTPException(status_code=429, detail=f"Rate limited. {status['tokens_available']} tokens remaining, refilling at {status['refill_rate']}/sec")
    
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread.get("is_archived"):
        raise HTTPException(status_code=400, detail="Cannot send messages to archived thread")

    mentions_spencer = "@Spencer" in req.content or req.content.lower().startswith("spencer")
    requires_human = req.structured.get("requires_human", False) if req.structured else False

    msg_id = save_thread_message(thread_id, thread["parent_channel_id"], agent["name"],
                                 req.content, req.structured, requires_human, mentions_spencer,
                                 req.image_url, req.image_type)

    await notify_mentions_ws(thread["parent_channel_id"], agent["name"], req.content, msg_id)

    # Broadcast to parent channel subscribers with thread context
    await broadcast_to_room(thread["parent_channel_id"], {
        "type": "message",
        "id": msg_id,
        "room_id": thread["parent_channel_id"],
        "thread_id": thread_id,
        "sender": agent["name"],
        "content": req.content,
        "structured": req.structured,
        "requires_human": requires_human,
        "mentions_spencer": mentions_spencer,
        "image_url": req.image_url,
        "image_type": req.image_type,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    return {"status": "sent", "id": msg_id, "thread_id": thread_id}


@app.get("/api/v2/threads/{thread_id}/messages")
async def get_thread_messages_endpoint(thread_id: str, token: str, limit: int = 50, before: str = None):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = get_thread_messages(thread_id, limit, before)
    return {"thread_id": thread_id, "messages": messages}


@app.put("/api/v2/threads/{thread_id}/messages/{msg_id}")
async def edit_thread_message(thread_id: str, msg_id: str, token: str, req: SendThreadMessageRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    # Verify message ownership
    sender = get_message_sender(msg_id)
    if sender and sender != agent["name"] and agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only the message author or an admin can edit")
    if edit_message(msg_id, req.content):
        mentions_spencer = "@Spencer" in req.content
        create_notifications_for_message(msg_id, thread["parent_channel_id"], agent["name"], req.content, mentions_spencer)
        await notify_mentions_ws(thread["parent_channel_id"], agent["name"], req.content, msg_id)
        await broadcast_to_room(thread["parent_channel_id"], {
            "type": "message", "edit": True, "id": msg_id,
            "room_id": thread["parent_channel_id"], "thread_id": thread_id,
            "sender": agent["name"], "content": req.content,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return {"status": "edited"}
    raise HTTPException(status_code=404, detail="Message not found or already deleted")


@app.delete("/api/v2/threads/{thread_id}/messages/{msg_id}")
async def delete_thread_message(thread_id: str, msg_id: str, token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    # Verify message ownership
    sender = get_message_sender(msg_id)
    if sender and sender != agent["name"] and agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only the message author or an admin can delete")
    if delete_message(msg_id):
        await broadcast_to_room(thread["parent_channel_id"], {
            "type": "message", "delete": True, "id": msg_id,
            "room_id": thread["parent_channel_id"], "thread_id": thread_id,
            "sender": agent["name"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Message not found")


@app.post("/api/v2/channels/{room_id}/messages")
async def send_channel_message(room_id: str, token: str, req: SendMessageRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    if not await rate_limiter.consume(agent["name"]):
        status = await rate_limiter.get_status(agent["name"])
        raise HTTPException(status_code=429, detail=f"Rate limited. {status['tokens_available']} tokens remaining, refilling at {status['refill_rate']}/sec")
    
    mentions_spencer = "@Spencer" in req.content or req.content.lower().startswith("spencer")
    requires_human = req.structured.get("requires_human", False) if req.structured else False
    
    msg_id = save_message(room_id, agent["name"], req.content, req.structured,
                          requires_human, mentions_spencer, req.image_url, req.image_type)

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
        "image_url": req.image_url,
        "image_type": req.image_type,
        "timestamp": datetime.now(timezone.utc).isoformat()
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
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM room_members WHERE room_id = ? AND agent_name = ?", (room_id, agent["name"]))

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
    # Verify message ownership
    sender = get_message_sender(msg_id)
    if sender and sender != agent["name"] and agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only the message author or an admin can edit")
    if edit_message(msg_id, req.content):
        mentions_spencer = "@Spencer" in req.content
        create_notifications_for_message(msg_id, room_id, agent["name"], req.content, mentions_spencer)
        await notify_mentions_ws(room_id, agent["name"], req.content, msg_id)
        await broadcast_to_room(room_id, {
            "type": "message", "edit": True, "id": msg_id,
            "room_id": room_id, "sender": agent["name"], "content": req.content,
            "timestamp": datetime.now(timezone.utc).isoformat()
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
    # Verify message ownership
    sender = get_message_sender(msg_id)
    if sender and sender != agent["name"] and agent["name"] not in ("Data", "Spencer"):
        raise HTTPException(status_code=403, detail="Only the message author or an admin can delete")
    if delete_message(msg_id):
        await broadcast_to_room(room_id, {
            "type": "message", "delete": True, "id": msg_id,
            "room_id": room_id, "sender": agent["name"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Message not found")


@app.post("/api/v2/dms/{other_agent}/messages")
async def send_dm_message(other_agent: str, token: str, req: SendMessageRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    if not await rate_limiter.consume(agent["name"]):
        status = await rate_limiter.get_status(agent["name"])
        raise HTTPException(status_code=429, detail=f"Rate limited. {status['tokens_available']} tokens remaining, refilling at {status['refill_rate']}/sec")
    
    try:
        room_id = create_dm(agent["name"], other_agent)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    mentions_spencer = "@Spencer" in req.content
    requires_human = req.structured.get("requires_human", False) if req.structured else False

    msg_id = save_message(room_id, agent["name"], req.content, req.structured,
                          requires_human, mentions_spencer, req.image_url, req.image_type)

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
        "image_url": req.image_url,
        "image_type": req.image_type,
        "timestamp": datetime.now(timezone.utc).isoformat()
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


class MarkReadRequest(BaseModel):
    """Mark messages in a room as read."""
    room_id: str = Field(description="Room ID to mark as read")


class StatusUpdateRequest(BaseModel):
    """Update agent presence status and details."""
    status_detail: Optional[str] = Field(default=None, description="Status: idle, busy, error, typing, in_call, debugging, deploying")
    status_message: Optional[str] = Field(default=None, description="Free-form status message")


@app.post("/api/v2/presence/status")
async def update_agent_status(token: str, req: StatusUpdateRequest):
    """Update extended presence status (detail + custom message)."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    set_presence(agent["name"], 
                 status=presence_mgr.get_status(agent["name"]),
                 status_detail=req.status_detail,
                 status_message=req.status_message)
    # Broadcast status update
    await presence_mgr.broadcast_presence(agent["name"], presence_mgr.get_status(agent["name"]))
    return {"status": "ok", "agent": agent["name"], "status_detail": req.status_detail, "status_message": req.status_message}

@app.post("/api/v2/read")
async def mark_read(token: str, req: MarkReadRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    mark_room_read(agent["name"], req.room_id)
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


@app.post("/api/v2/presence/heartbeat")
async def send_heartbeat(token: str):
    """Agent heartbeat endpoint - confirms liveness."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    await presence_mgr.record_heartbeat(agent["name"])
    return {"status": "ok", "agent": agent["name"], "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/v2/presence/reconnect")
async def get_reconnect_info(token: str):
    """Get reconnect backoff info for an agent."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    delay = presence_mgr.get_reconnect_delay(agent["name"])
    attempts = presence_mgr.reconnect_attempts.get(agent["name"], 0)
    return {"agent": agent["name"], "reconnect_attempts": attempts, "backoff_seconds": delay}


@app.get("/api/v2/messages/{msg_id}/ack")
async def get_message_ack(msg_id: str, token: str):
    """Get delivery acknowledgment status for a message."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    status = get_message_ack_status(msg_id)
    return status


@app.get("/api/v2/messages/unacked")
async def get_my_unacked_messages(token: str, limit: int = 20):
    """Get messages sent by this agent that haven't been fully acknowledged."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    unacked = get_unacked_messages(agent["name"], limit)
    return {"unacked": unacked, "count": len(unacked)}


@app.get("/api/v2/ratelimit")
async def get_rate_limit_status(token: str):
    """Get current rate limit status for the authenticated agent."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    status = await rate_limiter.get_status(agent["name"])
    return {"agent": agent["name"], **status}


class SearchRequest(BaseModel):
    """Full-text search across messages using FTS5."""
    q: str = Field(description="Search query (FTS5 syntax supported)")
    room_id: Optional[str] = Field(default=None, description="Filter by room ID")
    sender: Optional[str] = Field(default=None, description="Filter by sender name")
    limit: int = Field(default=50, description="Max results to return")


@app.post("/api/v2/search")
async def search_messages_endpoint(token: str, req: SearchRequest):
    """Full-text search across messages using FTS5."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    if not req.q or len(req.q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Search query must be at least 2 characters")
    results = search_messages(req.q, room_id=req.room_id, sender=req.sender, limit=req.limit)
    return {"query": req.q, "results": results, "count": len(results)}


@app.get("/api/v2/admin/banner")
async def admin_get_banner(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    return {"text": get_banner()}


class BannerUpdate(BaseModel):
    """Update the global channel banner text (Spencer/Data only)."""
    text: str = Field(description="Banner text to display")

@app.put("/api/v2/admin/banner")
async def admin_set_banner(token: str, req: BannerUpdate):
    agent = get_agent_by_token(token)
    if not agent or agent["name"] not in ("Spencer", "Data"):
        raise HTTPException(status_code=403, detail="Not authorized")
    set_banner(req.text)
    return {"status": "updated", "text": req.text}


@app.get("/api/v2/metrics")
async def get_metrics(token: str):
    """Get API health metrics — request counts, broadcast latency, connection stats."""
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    stats = metrics.get_stats()
    # Add real-time presence stats
    stats["total_agents"] = len(presence_mgr.connections)
    stats["total_heartbeats_tracked"] = len(presence_mgr.heartbeats)
    return stats


@app.post("/api/v2/webhooks/register")
async def register_webhook(token: str, req: WebhookRegisterRequest):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO webhooks (agent_name, url, events)
               VALUES (?, ?, ?)
               ON CONFLICT(agent_name) DO UPDATE SET
               url=excluded.url, events=excluded.events""",
            (agent["name"], req.url, json.dumps(req.events))
        )

        return {"status": "registered"}


@app.delete("/api/v2/webhooks")
async def unregister_webhook(token: str):
    agent = get_agent_by_token(token)
    if not agent:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    with get_db(row_factory=False) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM webhooks WHERE agent_name = ?", (agent["name"],))

        return {"status": "unregistered"}


# --- Webhook Delivery ---

async def deliver_webhooks(room_id: str, message: dict):
    """Fire registered webhooks when a message is broadcast."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT agent_name, url, events FROM webhooks")
            webhooks = cursor.fetchall()
    except Exception as e:
        logger.warning(f"Webhook delivery: failed to load webhooks: {e}")
        return

    if not webhooks:
        return

    # Determine event type
    event_type = "message"
    sender = message.get("sender")
    content = message.get("content", "")

    webhook_tasks = []
    for agent_name, url, events_json in webhooks:
        try:
            events = json.loads(events_json) if events_json else ["all"]
            # Check if this webhook should fire for this event
            if "all" in events or event_type in events:
                payload = {
                    "event": event_type,
                    "room_id": room_id,
                    "sender": sender,
                    "message_id": message.get("id"),
                    "content": content[:500],  # Truncate for payload size
                    "timestamp": message.get("created_at"),
                    "target_agent": agent_name
                }
                webhook_tasks.append(_fire_webhook(url, payload))
        except Exception as e:
            logger.warning(f"Webhook delivery: error processing webhook for {agent_name}: {e}")

    if webhook_tasks:
        await asyncio.gather(*webhook_tasks, return_exceptions=True)

async def _fire_webhook(url: str, payload: dict):
    """Fire a single webhook with retry."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info(f"Webhook delivered to {url}: {payload['event']}")
            else:
                logger.warning(f"Webhook failed {resp.status_code} -> {url}")
    except Exception as e:
        logger.warning(f"Webhook delivery error -> {url}: {e}")


# --- WebSocket ---

async def broadcast_to_room(room_id: str, message: dict):
    subscribers = await presence_mgr.get_subscribers(room_id)
    logger.info(f"Broadcast room={room_id} subscribers={len(subscribers)}")
    disconnected = []
    start = time.monotonic()
    for agent_name, websocket in subscribers:
        try:
            await websocket.send_json(message)
            logger.debug(f"Broadcast sent to {agent_name}")
            # Auto-record ack for successfully delivered messages
            msg_id = message.get("id")
            if msg_id:
                record_message_ack(msg_id, agent_name)
        except Exception as e:
            logger.warning(f"Broadcast failed for {agent_name}: {e}")
            disconnected.append((agent_name, websocket))
    latency_ms = (time.monotonic() - start) * 1000
    metrics.record_broadcast(len(subscribers), latency_ms)

    # Fire webhooks in background (don't block broadcast)
    asyncio.create_task(deliver_webhooks(room_id, message))

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
    
    # Track reconnect attempts and calculate backoff
    was_connected = agent_name in presence_mgr.connections and len(presence_mgr.connections.get(agent_name, [])) > 0
    reconnect_info = None
    if was_connected or agent_name in presence_mgr.reconnect_attempts:
        reconnect_info = await presence_mgr.handle_reconnect(agent_name)
    
    await presence_mgr.connect(agent_name, websocket)
    metrics.record_ws_connect()
    
    try:
        # Auto-join agent to default channels
        with get_db(row_factory=False) as conn:
            cursor = conn.cursor()
            for room_id in ["general", "agenttracker", "ops"]:
                cursor.execute(
                    "INSERT OR IGNORE INTO room_members (room_id, agent_name) VALUES (?, ?)",
                    (room_id, agent_name)
                )

        # Send welcome with agent's rooms and current banner
        rooms = get_agent_rooms(agent_name)
        room_ids = [r["id"] for r in rooms]
        welcome_msg = {
            "type": "system",
            "event": "connected",
            "agent": agent_name,
            "rooms": room_ids,
            "banner": get_banner(),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        if reconnect_info:
            welcome_msg["reconnect"] = reconnect_info
            welcome_msg["reconnected"] = True
        await websocket.send_json(welcome_msg)

        # Auto-subscribe to all rooms so clients never miss messages
        await presence_mgr.subscribe(agent_name, websocket, room_ids)
        logger.info(f"WS auto-subscribe {agent_name} to {room_ids}")
        
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                action = msg.get("action")
                
                if action == "subscribe":
                    rooms = msg.get("rooms", [])
                    await presence_mgr.subscribe(agent_name, websocket, rooms)
                    logger.info(f"WS subscribe {agent_name} to {rooms}")
                    await websocket.send_json({
                        "type": "system",
                        "event": "subscribed",
                        "rooms": rooms
                    })
                
                elif action == "typing":
                    await presence_mgr.set_typing(agent_name)
                    # Broadcast typing to all room subscribers (UX-005)
                    room_id = msg.get("room_id")
                    if room_id:
                        subscribers = await presence_mgr.get_subscribers(room_id)
                        for sub_agent, sub_ws in subscribers:
                            if sub_agent != agent_name:
                                try:
                                    await sub_ws.send_json({
                                        "type": "typing",
                                        "sender": agent_name,
                                        "room_id": room_id,
                                        "timestamp": datetime.now(timezone.utc).isoformat()
                                    })
                                except Exception:
                                    pass
                
                elif action == "heartbeat":
                    await presence_mgr.record_heartbeat(agent_name)
                
                elif action == "ack":
                    # Message delivery acknowledgment
                    message_id = msg.get("message_id")
                    if message_id:
                        record_message_ack(message_id, agent_name)
                        await websocket.send_json({
                            "type": "system",
                            "event": "ack_recorded",
                            "message_id": message_id,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                        # Check if all recipients have acked, notify the sender
                        ack_status = get_message_ack_status(message_id)
                        if ack_status["all_acked"]:
                            # Find the original sender and notify them
                            with get_db(row_factory=False) as conn:
                                cursor = conn.cursor()
                                cursor.execute("SELECT sender FROM messages WHERE id = ?", (message_id,))
                                row = cursor.fetchone()

                            if row:
                                sender_name = row[0]
                                notification = {
                                    "type": "system",
                                    "event": "message_delivered",
                                    "message_id": message_id,
                                    "all_recipients_acked": True,
                                    "timestamp": datetime.now(timezone.utc).isoformat()
                                }
                                for ws in presence_mgr.connections.get(sender_name, []):
                                    try:
                                        await ws.send_json(notification)
                                    except Exception:
                                        pass

                elif action == "reaction":
                    # Add/remove reaction via WebSocket
                    message_id = msg.get("message_id")
                    emoji = msg.get("emoji")
                    remove = msg.get("remove", False)
                    if not message_id or not emoji:
                        continue
                    if remove:
                        result = remove_reaction(message_id, agent_name, emoji)
                        event_type = "removed"
                    else:
                        result = add_reaction(message_id, agent_name, emoji)
                        event_type = "added"
                    if "error" not in result:
                        await broadcast_to_room(result["room_id"], {
                            "type": "reaction",
                            "event": event_type,
                            "message_id": message_id,
                            "agent_name": agent_name,
                            "emoji": emoji,
                            "reactions": result["reactions"],
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })

                elif action == "pin":
                    # Pin/unpin message via WebSocket
                    room_id = msg.get("room_id")
                    message_id = msg.get("message_id")
                    unpin = msg.get("unpin", False)
                    if not room_id or not message_id:
                        continue
                    if unpin:
                        unpin_message(room_id, message_id)
                        await broadcast_to_room(room_id, {
                            "type": "pin",
                            "event": "removed",
                            "room_id": room_id,
                            "message_id": message_id,
                            "unpinned_by": agent_name,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                    else:
                        result = pin_message(message_id, room_id, agent_name)
                        if "error" not in result:
                            await broadcast_to_room(room_id, {
                                "type": "pin",
                                "event": "added",
                                "room_id": room_id,
                                "message_id": message_id,
                                "pinned_by": agent_name,
                                "pin": result,
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            })
                
                elif action == "send":
                    if not await rate_limiter.consume(agent_name):
                        await websocket.send_json({
                            "type": "system",
                            "event": "rate_limited",
                            "detail": "Too many messages. Slow down.",
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                        continue
                    
                    room_id = msg.get("room_id", "general")
                    content = msg.get("content", "")
                    structured = msg.get("structured")
                    image_url = msg.get("image_url")
                    image_type = msg.get("image_type")
                    
                    mentions_spencer = "@Spencer" in content
                    requires_human = structured.get("requires_human", False) if structured else False
                    
                    msg_id = save_message(room_id, agent_name, content, structured,
                                          requires_human, mentions_spencer, image_url, image_type)

                    await broadcast_to_room(room_id, {
                        "type": "message",
                        "id": msg_id,
                        "room_id": room_id,
                        "sender": agent_name,
                        "content": content,
                        "structured": structured,
                        "requires_human": requires_human,
                        "mentions_spencer": mentions_spencer,
                        "image_url": image_url,
                        "image_type": image_type,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
                    # Confirm message was sent and broadcast
                    await websocket.send_json({
                        "type": "system",
                        "event": "message_sent",
                        "message_id": msg_id,
                        "room_id": room_id,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })

                elif action == "presence":
                    status = msg.get("status", "online")
                    current_room = msg.get("current_room")
                    set_presence(agent_name, status, current_room)
                
                elif action == "subscribe_thread":
                    thread_id = msg.get("thread_id")
                    if thread_id:
                        thread = get_thread(thread_id)
                        if thread:
                            join_thread(thread_id, agent_name)
                            await presence_mgr.subscribe(agent_name, websocket, [thread["parent_channel_id"]])
                            logger.info(f"WS subscribe {agent_name} to thread {thread_id}")
                            await websocket.send_json({
                                "type": "system", "event": "thread_subscribed",
                                "thread_id": thread_id, "rooms": [thread["parent_channel_id"]]
                            })
                
                elif action == "send_thread":
                    if not await rate_limiter.consume(agent_name):
                        await websocket.send_json({
                            "type": "system",
                            "event": "rate_limited",
                            "detail": "Too many messages. Slow down.",
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                        continue

                    thread_id = msg.get("thread_id")
                    content = msg.get("content", "")
                    structured = msg.get("structured")
                    image_url = msg.get("image_url")
                    image_type = msg.get("image_type")
                    thread = get_thread(thread_id) if thread_id else None
                    if thread and not thread.get("is_archived"):
                        mentions_spencer = "@Spencer" in content
                        requires_human = structured.get("requires_human", False) if structured else False
                        msg_id = save_thread_message(thread_id, thread["parent_channel_id"], agent_name,
                                                     content, structured, requires_human, mentions_spencer,
                                                     image_url, image_type)
                        await notify_mentions_ws(thread["parent_channel_id"], agent_name, content, msg_id)
                        await broadcast_to_room(thread["parent_channel_id"], {
                            "type": "message", "id": msg_id,
                            "room_id": thread["parent_channel_id"], "thread_id": thread_id,
                            "sender": agent_name, "content": content,
                            "structured": structured, "requires_human": requires_human,
                            "mentions_spencer": mentions_spencer,
                            "image_url": image_url,
                            "image_type": image_type,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                        # Notify sender via ack that thread message was broadcast
                        await websocket.send_json({
                            "type": "system",
                            "event": "message_sent",
                            "message_id": msg_id,
                            "thread_id": thread_id,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                
            except json.JSONDecodeError:
                pass
    
    except WebSocketDisconnect:
        await presence_mgr.disconnect(agent_name, websocket)
        metrics.record_ws_disconnect()
    except Exception as e:
        logger.error(f"WebSocket error for {agent_name}: {e}")
        await presence_mgr.disconnect(agent_name, websocket)
        metrics.record_ws_disconnect()


# --- Spencer Relay Endpoint (Data only) ---

@app.get("/api/v2/relay")
async def get_relay_queue(token: str):
    agent = get_agent_by_token(token)
    if not agent or agent["name"] != "Data":
        raise HTTPException(status_code=403, detail="Invalid token")
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT m.* FROM messages m
               WHERE (m.mentions_spencer = TRUE OR m.requires_human = TRUE)
               AND m.timestamp > datetime('now', '-24 hours')
               ORDER BY m.timestamp DESC LIMIT 50"""
        )
        rows = cursor.fetchall()

        return {"pending": [dict(r) for r in rows]}


# --- File watcher for live HTML reloads ---
HTML_PATH = os.path.join(os.path.dirname(__file__), "v2_chat.html")
_html_last_mtime = os.path.getmtime(HTML_PATH)

async def _broadcast_reload():
    await presence_mgr.broadcast_all({"type": "reload", "timestamp": datetime.now(timezone.utc).isoformat()})

def _watch_html():
    global _html_last_mtime
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while not _shutdown_event.is_set():
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


# --- Heartbeat timeout checker (FEAT-002) ---
def _check_heartbeat_timeouts():
    """Periodically check for agents with stale heartbeats and mark them away.
    Also purges stale entries from RateLimiter and PresenceManager.
    """
    purge_counter = 0
    while not _shutdown_event.is_set():
        time.sleep(HEARTBEAT_TIMEOUT_SECONDS)
        try:
            now = datetime.now(timezone.utc)
            stale_agents = []
            for agent_name, last_hb in list(presence_mgr.heartbeats.items()):
                if (now - last_hb).total_seconds() > HEARTBEAT_TIMEOUT_SECONDS:
                    conn_count = len(presence_mgr.connections.get(agent_name, []))
                    if conn_count > 0:
                        stale_agents.append(agent_name)
            for agent_name in stale_agents:
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.run_coroutine_threadsafe(
                        presence_mgr.broadcast_presence(agent_name, "away"), loop
                    )
                except RuntimeError:
                    pass

            # Purge stale dict entries every 10 cycles (~every 7.5 minutes)
            purge_counter += 1
            if purge_counter >= 10:
                purge_counter = 0
                rate_limiter.purge_stale(idle_seconds=3600)
                presence_mgr.purge_stale(idle_seconds=600)
        except Exception:
            pass


heartbeat_checker = threading.Thread(target=_check_heartbeat_timeouts, daemon=True)
heartbeat_checker.start()


# --- Message archival job (IMPROVE-010) ---
MESSAGE_ARCHIVE_DAYS = int(os.getenv("ACV2_ARCHIVE_DAYS", "30"))
MESSAGE_ARCHIVE_INTERVAL = int(os.getenv("ACV2_ARCHIVE_INTERVAL", "3600"))  # Run every hour


def _archive_old_messages():
    """Periodically soft-delete messages older than MESSAGE_ARCHIVE_DAYS."""
    while not _shutdown_event.is_set():
        time.sleep(MESSAGE_ARCHIVE_INTERVAL)
        if _shutdown_event.is_set():
            break
        try:
            with get_db(row_factory=False) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """UPDATE messages SET is_deleted = TRUE, content = '[archived]'
                       WHERE is_deleted = FALSE
                       AND timestamp < datetime('now', ?)
                       AND requires_human = FALSE""",
                    (f"-{MESSAGE_ARCHIVE_DAYS} days",)
                )
                archived = cursor.rowcount

                if archived > 0:
                    logger.info(f"Archive: soft-deleted {archived} messages older than {MESSAGE_ARCHIVE_DAYS} days")
        except Exception as e:
            logger.error(f"Archive daemon error: {e}")


archive_thread = threading.Thread(target=_archive_old_messages, daemon=True)
archive_thread.start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
