"""Test configuration and fixtures"""
import pytest
import pytest_asyncio
import os
import sqlite3
from httpx import ASGITransport, AsyncClient
from main_v2 import app, init_db, load_agents, AGENT_TOKENS, DATABASE

# Use a test database
TEST_DB = "test_chat_v2.db"

@pytest.fixture(autouse=True)
def setup_test_db():
    """Set up test database before each test"""
    # Backup original DATABASE path
    import main_v2
    original_db = main_v2.DATABASE
    
    # Use test database
    main_v2.DATABASE = TEST_DB
    
    # Initialize test database
    init_db()
    load_agents()
    
    yield
    
    # Cleanup test database
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    
    # Restore original DATABASE path
    main_v2.DATABASE = original_db

@pytest_asyncio.fixture
async def client():
    """Create async test client"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
