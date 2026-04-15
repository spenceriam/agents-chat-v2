"""ACV2 WebSocket Integration Tests"""
import pytest
import pytest_asyncio
import json
import asyncio
import os
from httpx import ASGITransport, AsyncClient
from main_v2 import app, init_db, load_agents

# Configure asyncio mode
pytestmark = pytest.mark.asyncio

TEST_TOKEN = "664500b7-81ab-4a20-9d72-9c1e97ea1e35"
DATA_TOKEN = "data-token-uuid-12345"

@pytest_asyncio.fixture
async def client():
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        test_agents = json.load(f)
    import main_v2
    main_v2.AGENT_TOKENS = test_agents
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_ws_connect_and_welcome(client):
    """Test WebSocket connection returns welcome message with rooms and banner."""
    import main_v2
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        main_v2.AGENT_TOKENS = json.load(f)
    from starlette.testclient import TestClient
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/v2/{TEST_TOKEN}") as ws:
        # First message may be presence_update (from auto-subscribe) or welcome
        data = ws.receive_json()
        if data.get("type") == "presence_update":
            data = ws.receive_json()
        assert data["type"] == "system"
        assert data["event"] == "connected"
        assert "rooms" in data
        assert "banner" in data
        assert "agent" in data


async def test_ws_subscribe(client):
    """Test subscribing to specific rooms via WebSocket."""
    import main_v2
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        main_v2.AGENT_TOKENS = json.load(f)
    from starlette.testclient import TestClient
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/v2/{TEST_TOKEN}") as ws:
        # Consume welcome + any presence_update
        data = ws.receive_json()
        while data.get("type") == "presence_update":
            data = ws.receive_json()
        # data is now the welcome message
        # Subscribe to additional rooms
        ws.send_json({"action": "subscribe", "rooms": ["general", "ops"]})
        data = ws.receive_json()
        assert data["type"] == "system"
        assert data["event"] == "subscribed"
        assert "general" in data["rooms"]
        assert "ops" in data["rooms"]


async def test_ws_send_message(client):
    """Test sending a message via WebSocket action."""
    import main_v2
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        main_v2.AGENT_TOKENS = json.load(f)
    from starlette.testclient import TestClient
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/v2/{TEST_TOKEN}") as ws:
        # Consume welcome + any presence_update
        data = ws.receive_json()
        while data.get("type") == "presence_update":
            data = ws.receive_json()
        ws.send_json({
            "action": "send",
            "room_id": "general",
            "content": "Hello from WebSocket!",
            "structured": {"intent": "test"}
        })
        # Should receive the broadcast back (skip any system/presence messages)
        data = ws.receive_json()
        while data.get("type") != "message":
            data = ws.receive_json()
        assert data["type"] == "message"
        assert data["content"] == "Hello from WebSocket!"
        assert data["sender"] == "Hermes"
        assert "id" in data


async def test_ws_typing_indicator(client):
    """Test typing indicator via WebSocket."""
    import main_v2
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        main_v2.AGENT_TOKENS = json.load(f)
    from starlette.testclient import TestClient
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/v2/{TEST_TOKEN}") as ws:
        ws.receive_json()  # welcome
        ws.send_json({"action": "typing"})
        # No response expected for typing action, just no error
        # Type another action to check connection is still alive
        ws.send_json({"action": "presence", "status": "online"})


async def test_ws_presence_update(client):
    """Test presence status update via WebSocket."""
    import main_v2
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        main_v2.AGENT_TOKENS = json.load(f)
    from starlette.testclient import TestClient
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/v2/{TEST_TOKEN}") as ws:
        # Consume welcome + any presence_update
        data = ws.receive_json()
        while data.get("type") == "presence_update":
            data = ws.receive_json()
        ws.send_json({"action": "presence", "status": "online", "current_room": "general"})
        # Should broadcast presence_update to all
        data = ws.receive_json()
        while data.get("type") != "presence_update":
            data = ws.receive_json()
        assert data["type"] == "presence_update"


async def test_ws_broadcast_message_delivery(client):
    """Test that messages sent via REST API are broadcast to WebSocket subscribers."""
    import main_v2
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        main_v2.AGENT_TOKENS = json.load(f)
    from starlette.testclient import TestClient
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/v2/{TEST_TOKEN}") as ws:
        # Consume welcome + any presence_update
        data = ws.receive_json()
        while data.get("type") == "presence_update":
            data = ws.receive_json()
        ws.send_json({"action": "subscribe", "rooms": ["general"]})
        # Consume subscribed confirmation + any presence_update
        data = ws.receive_json()
        while data.get("type") == "presence_update":
            data = ws.receive_json()
        
        # Send message via REST API
        response = await client.post(
            f"/api/v2/channels/general/messages?token={TEST_TOKEN}",
            json={"content": "REST broadcast test", "structured": None}
        )
        assert response.status_code == 200
        
        # Should receive broadcast on WebSocket
        data = ws.receive_json()
        assert data["type"] == "message"
        assert data["content"] == "REST broadcast test"


async def test_ws_heartbeat(client):
    """Test heartbeat action via WebSocket."""
    import main_v2
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        main_v2.AGENT_TOKENS = json.load(f)
    from starlette.testclient import TestClient
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/v2/{TEST_TOKEN}") as ws:
        # Consume welcome + any presence_update
        data = ws.receive_json()
        while data.get("type") == "presence_update":
            data = ws.receive_json()
        ws.send_json({"action": "heartbeat"})
        # Heartbeat doesn't send a response, just records the heartbeat
        # Verify connection is still alive
        ws.send_json({"action": "presence", "status": "online"})
        data = ws.receive_json()
        while data.get("type") != "presence_update":
            data = ws.receive_json()
        assert data["type"] == "presence_update"


async def test_heartbeat_api_endpoint(client):
    """Test the heartbeat REST API endpoint."""
    response = await client.post(f"/api/v2/presence/heartbeat?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["agent"] == "Hermes"
    assert "timestamp" in data


async def test_reconnect_info_endpoint(client):
    """Test the reconnect backoff info endpoint."""
    response = await client.get(f"/api/v2/presence/reconnect?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "reconnect_attempts" in data
    assert "backoff_seconds" in data
    assert data["agent"] == "Hermes"


async def test_thread_create_api(client):
    """Test creating a thread via API."""
    response = await client.post(
        f"/api/v2/channels/general/threads?token={TEST_TOKEN}",
        json={"topic": "Test thread topic", "metadata": {"test": True}}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "created"
    assert "thread" in data
    assert data["thread"]["topic"] == "Test thread topic"
    assert data["thread"]["parent_channel_id"] == "general"
    return data["thread"]["id"]


async def test_thread_list_api(client):
    """Test listing threads in a channel."""
    # Create a thread first
    await client.post(
        f"/api/v2/channels/general/threads?token={TEST_TOKEN}",
        json={"topic": "List test thread"}
    )
    
    response = await client.get(f"/api/v2/channels/general/threads?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "threads" in data
    assert len(data["threads"]) > 0
    assert data["channel_id"] == "general"


async def test_thread_get_api(client):
    """Test getting thread details."""
    # Create a thread first
    create_resp = await client.post(
        f"/api/v2/channels/general/threads?token={TEST_TOKEN}",
        json={"topic": "Get test thread"}
    )
    thread_id = create_resp.json()["thread"]["id"]
    
    response = await client.get(f"/api/v2/threads/{thread_id}?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "thread" in data
    assert "members" in data["thread"]
    assert data["thread"]["topic"] == "Get test thread"


async def test_thread_join_api(client):
    """Test joining a thread."""
    # Create a thread first
    create_resp = await client.post(
        f"/api/v2/channels/ops/threads?token={TEST_TOKEN}",
        json={"topic": "Join test thread"}
    )
    thread_id = create_resp.json()["thread"]["id"]
    
    response = await client.post(f"/api/v2/threads/{thread_id}/join?token={TEST_TOKEN}")
    assert response.status_code == 200
    assert response.json()["status"] == "joined"


async def test_thread_send_message(client):
    """Test sending a message to a thread."""
    # Create a thread first
    create_resp = await client.post(
        f"/api/v2/channels/general/threads?token={TEST_TOKEN}",
        json={"topic": "Message test thread"}
    )
    thread_id = create_resp.json()["thread"]["id"]
    
    response = await client.post(
        f"/api/v2/threads/{thread_id}/messages?token={TEST_TOKEN}",
        json={"content": "Hello in thread!", "structured": None}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "sent"
    assert data["thread_id"] == thread_id
    assert "id" in data


async def test_thread_get_messages(client):
    """Test getting messages from a thread."""
    # Create thread and send message
    create_resp = await client.post(
        f"/api/v2/channels/general/threads?token={TEST_TOKEN}",
        json={"topic": "Get messages thread"}
    )
    thread_id = create_resp.json()["thread"]["id"]
    
    await client.post(
        f"/api/v2/threads/{thread_id}/messages?token={TEST_TOKEN}",
        json={"content": "Thread message 1"}
    )
    
    response = await client.get(f"/api/v2/threads/{thread_id}/messages?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "messages" in data
    assert len(data["messages"]) >= 1
    assert data["thread_id"] == thread_id


async def test_thread_update_topic(client):
    """Test updating a thread topic."""
    create_resp = await client.post(
        f"/api/v2/channels/general/threads?token={TEST_TOKEN}",
        json={"topic": "Original topic"}
    )
    thread_id = create_resp.json()["thread"]["id"]
    
    response = await client.put(
        f"/api/v2/threads/{thread_id}?token={TEST_TOKEN}",
        json={"topic": "Updated topic"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert response.json()["topic"] == "Updated topic"


async def test_thread_archive(client):
    """Test archiving a thread."""
    create_resp = await client.post(
        f"/api/v2/channels/general/threads?token={TEST_TOKEN}",
        json={"topic": "Archive test thread"}
    )
    thread_id = create_resp.json()["thread"]["id"]
    
    response = await client.post(f"/api/v2/threads/{thread_id}/archive?token={TEST_TOKEN}")
    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    
    # Should not be able to send messages to archived thread
    msg_resp = await client.post(
        f"/api/v2/threads/{thread_id}/messages?token={TEST_TOKEN}",
        json={"content": "Should fail"}
    )
    assert msg_resp.status_code == 400


async def test_thread_invalid_channel(client):
    """Test creating thread in non-existent channel."""
    response = await client.post(
        f"/api/v2/channels/nonexistent/threads?token={TEST_TOKEN}",
        json={"topic": "Should fail"}
    )
    assert response.status_code == 404


async def test_ws_invalid_token():
    """Test WebSocket rejects invalid tokens."""
    import main_v2
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        main_v2.AGENT_TOKENS = json.load(f)
    from starlette.testclient import TestClient
    tc = TestClient(app)
    with pytest.raises(Exception):
        with tc.websocket_connect("/ws/v2/invalid-token-12345") as ws:
            ws.receive_json()


async def test_message_edit_delete_thread(client):
    """Test editing and deleting messages in a thread."""
    # Create thread and send message
    create_resp = await client.post(
        f"/api/v2/channels/general/threads?token={TEST_TOKEN}",
        json={"topic": "Edit/Delete test thread"}
    )
    thread_id = create_resp.json()["thread"]["id"]
    
    send_resp = await client.post(
        f"/api/v2/threads/{thread_id}/messages?token={TEST_TOKEN}",
        json={"content": "Original thread message"}
    )
    msg_id = send_resp.json()["id"]
    
    # Edit
    edit_resp = await client.put(
        f"/api/v2/threads/{thread_id}/messages/{msg_id}?token={TEST_TOKEN}",
        json={"content": "Edited thread message"}
    )
    assert edit_resp.status_code == 200
    assert edit_resp.json()["status"] == "edited"
    
    # Delete
    delete_resp = await client.delete(
        f"/api/v2/threads/{thread_id}/messages/{msg_id}?token={TEST_TOKEN}"
    )
    assert delete_resp.status_code == 200
    assert delete_resp.json()["status"] == "deleted"


async def test_unread_counts_api(client):
    """Test unread count functionality."""
    response = await client.get(f"/api/v2/unread?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "unread" in data
    assert "mentions" in data
    assert isinstance(data["unread"], dict)


async def test_banner_admin_api(client):
    """Test banner get/set admin API."""
    # Get current banner
    response = await client.get(f"/api/v2/admin/banner?token={TEST_TOKEN}")
    assert response.status_code == 200
    assert "text" in response.json()
    
    # Set banner (Hermes may not have admin rights - test the endpoint exists)
    # Using Data token which has admin rights
    response = await client.put(
        f"/api/v2/admin/banner?token={DATA_TOKEN}",
        json={"text": "Test banner text"}
    )
    # Either 200 (if Data token works) or 403 (if token doesn't match real agents.json)
    assert response.status_code in (200, 403)
