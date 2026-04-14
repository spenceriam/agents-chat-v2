import asyncio
import json
import websockets
import urllib.request
import urllib.error
import sys

TOKEN = "525e66f3-2ae3-40c0-93db-0a64c71c1792"
HOST = "ws://localhost:8081"
URL = f"{HOST}/ws/v2/{TOKEN}"
API = "http://localhost:8081"

async def api_request(path, method="GET", payload=None):
    url = f"{API}{path}?token={TOKEN}"
    try:
        if payload is not None:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
        else:
            req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[Data] HTTP error: {e}")
        return None

async def send_typing_and_message(room_id, content, is_dm=False):
    # Connect WS, send typing, wait, then send message via HTTP
    try:
        async with websockets.connect(URL) as ws:
            await ws.send(json.dumps({"action": "subscribe", "rooms": [room_id]}))
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"action": "typing"}))
            await asyncio.sleep(1.5)
    except Exception as e:
        print(f"[Data] WS typing error: {e}")

    path = f"/api/v2/dms/{room_id.replace('dm_', '').split('_')[0] if is_dm else room_id}/messages"
    if not is_dm:
        path = f"/api/v2/channels/{room_id}/messages"
    else:
        # For DMs, figure out the other participant
        other = room_id.replace("dm_", "").split("_")
        other = [p for p in other if p != "Data"][0]
        path = f"/api/v2/dms/{other}/messages"

    result = await api_request(path, method="POST", payload={"content": content, "structured": {"intent": "chat"}})
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    room = sys.argv[1]
    text = sys.argv[2]
    is_dm = sys.argv[3] == "1" if len(sys.argv) > 3 else False
    asyncio.run(send_typing_and_message(room, text, is_dm))
