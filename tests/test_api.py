"""ACV2 API Test Suite"""
import pytest
import pytest_asyncio
import json
import uuid
import os
from httpx import ASGITransport, AsyncClient
from main_v2 import app, init_db, load_agents, AGENT_TOKENS

# Configure asyncio mode
pytestmark = pytest.mark.asyncio

# Test tokens (will be loaded from agents.json in real deployment)
TEST_TOKEN = "664500b7-81ab-4a20-9d72-9c1e97ea1e35"
INVALID_TOKEN = "invalid-token-12345"

@pytest_asyncio.fixture
async def client():
    # Load test agents.json
    test_agents_path = os.path.join(os.path.dirname(__file__), "agents.json")
    with open(test_agents_path) as f:
        test_agents = json.load(f)
    
    # Patch AGENT_TOKENS for tests
    import main_v2
    main_v2.AGENT_TOKENS = test_agents
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

async def test_health_check(client):
    """Test health endpoint"""
    response = await client.get("/api/v2/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "2.0.0"

async def test_invalid_token(client):
    """Test endpoints with invalid token"""
    response = await client.get(f"/api/v2/me?token={INVALID_TOKEN}")
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid token"

async def test_get_me(client):
    """Test getting agent info"""
    response = await client.get(f"/api/v2/me?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Hermes"

async def test_list_channels(client):
    """Test listing channels"""
    response = await client.get(f"/api/v2/channels?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "channels" in data
    # Default channels should exist
    channel_ids = [c["id"] for c in data["channels"]]
    assert "general" in channel_ids
    assert "ops" in channel_ids
    assert "agenttracker" in channel_ids

async def test_send_channel_message(client):
    """Test sending a message to a channel"""
    msg_data = {
        "content": "Test message from Hermes",
        "structured": {"intent": "test"}
    }
    response = await client.post(
        f"/api/v2/channels/general/messages?token={TEST_TOKEN}",
        json=msg_data
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "sent"
    assert "id" in data

async def test_get_channel_messages(client):
    """Test retrieving channel messages"""
    response = await client.get(
        f"/api/v2/channels/general/messages?token={TEST_TOKEN}&limit=10"
    )
    assert response.status_code == 200
    data = response.json()
    assert "messages" in data
    assert isinstance(data["messages"], list)

async def test_send_dm(client):
    """Test sending a DM to another agent"""
    dm_data = {
        "content": "Test DM from Hermes to Data",
        "structured": {"intent": "test"}
    }
    response = await client.post(
        f"/api/v2/dms/Data/messages?token={TEST_TOKEN}",
        json=dm_data
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "sent"
    assert data["room_id"].startswith("dm_")

async def test_list_dms(client):
    """Test listing DMs"""
    response = await client.get(f"/api/v2/dms?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "dms" in data

async def test_get_dm_messages(client):
    """Test retrieving DM messages"""
    response = await client.get(
        f"/api/v2/dms/Data/messages?token={TEST_TOKEN}&limit=10"
    )
    assert response.status_code == 200
    data = response.json()
    assert "messages" in data
    assert "room_id" in data

async def test_get_notifications(client):
    """Test getting notifications"""
    response = await client.get(f"/api/v2/notifications?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "notifications" in data
    assert "unread_count" in data

async def test_clear_notifications(client):
    """Test clearing notifications"""
    response = await client.post(f"/api/v2/notifications/clear?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cleared"

async def test_mark_read(client):
    """Test marking a room as read"""
    response = await client.post(f"/api/v2/read?token={TEST_TOKEN}&room_id=general")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

async def test_get_unread(client):
    """Test getting unread counts"""
    response = await client.get(f"/api/v2/unread?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "unread" in data
    assert "mentions" in data

async def test_get_presence(client):
    """Test getting presence info"""
    response = await client.get(f"/api/v2/presence?token={TEST_TOKEN}")
    assert response.status_code == 200
    data = response.json()
    assert "presence" in data

async def test_message_edit(client):
    """Test editing a message"""
    # First send a message
    msg_data = {"content": "Original message", "structured": {"intent": "test"}}
    send_response = await client.post(
        f"/api/v2/channels/general/messages?token={TEST_TOKEN}",
        json=msg_data
    )
    msg_id = send_response.json()["id"]
    
    # Edit the message
    edit_data = {"content": "Edited message", "structured": {"intent": "test"}}
    edit_response = await client.put(
        f"/api/v2/channels/general/messages/{msg_id}?token={TEST_TOKEN}",
        json=edit_data
    )
    assert edit_response.status_code == 200
    assert edit_response.json()["status"] == "edited"

async def test_message_delete(client):
    """Test deleting a message"""
    # First send a message
    msg_data = {"content": "Message to delete", "structured": {"intent": "test"}}
    send_response = await client.post(
        f"/api/v2/channels/general/messages?token={TEST_TOKEN}",
        json=msg_data
    )
    msg_id = send_response.json()["id"]
    
    # Delete the message
    delete_response = await client.delete(
        f"/api/v2/channels/general/messages/{msg_id}?token={TEST_TOKEN}"
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "deleted"
