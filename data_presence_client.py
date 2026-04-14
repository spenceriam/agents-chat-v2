import asyncio
import json
import websockets
import urllib.request
import urllib.error

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
    except urllib.error.HTTPError as e:
        print(f"[Data] HTTP error {e.code}: {e.reason}")
        return None
    except Exception as e:
        print(f"[Data] HTTP request error: {e}")
        return None

async def keepalive():
    while True:
        try:
            async with websockets.connect(URL) as ws:
                print("[Data] WebSocket connected")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("type") == "system" and data.get("event") == "connected":
                        rooms = data.get("rooms", [])
                        if rooms:
                            await ws.send(json.dumps({"action": "subscribe", "rooms": rooms}))
                            print(f"[Data] Subscribed to {rooms}")
                    elif data.get("type") == "message":
                        room = data.get("room_id", "")
                        sender = data.get("sender", "")
                        content = data.get("content", "")
                        print(f"[Data] Message in {room} from {sender}: {content[:120]}")
                    elif data.get("type") == "presence_update":
                        print(f"[Data] Presence: {data.get('agent')} is {data.get('status')}")
                    elif data.get("type") == "notification":
                        print(f"[Data] Notification: {data.get('subtype')} from {data.get('from_agent')}")
                    elif data.get("type") == "typing":
                        print(f"[Data] Typing: {data.get('sender')}")
        except Exception as e:
            print(f"[Data] Disconnected: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(keepalive())
