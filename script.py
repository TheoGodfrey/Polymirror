#!/usr/bin/env python3
"""
Full WebSocket debug - see if trades actually come through.
Run this + watch_target.py side by side.
"""

import asyncio
import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv
import requests

load_dotenv()

try:
    import websockets
except ImportError:
    print("pip install websockets")
    exit(1)

TARGET = os.getenv("TARGET_ADDRESS", "").lower()
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

async def main():
    print(f"Target: {TARGET}")
    print(f"WS URL: {WS_URL}")
    print("=" * 60)
    
    # Get target's tokens
    r = requests.get(f"https://data-api.polymarket.com/positions?user={TARGET}&limit=50")
    positions = r.json()
    token_ids = [p.get("asset") for p in positions if p.get("asset")]
    
    print(f"Target has {len(positions)} positions")
    print(f"Subscribing to {len(token_ids)} token IDs")
    print("=" * 60)
    
    async with websockets.connect(WS_URL, ping_interval=30) as ws:
        # Subscribe
        sub = {"assets_ids": token_ids, "type": "market"}
        await ws.send(json.dumps(sub))
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Subscribed to market channel")
        print("=" * 60)
        print("LISTENING FOR ALL MESSAGES...")
        print("Open another terminal and run: python watch_target.py")
        print("When watch_target.py shows a trade, check if WS shows anything")
        print("=" * 60)
        
        msg_count = 0
        empty_count = 0
        start_time = time.time()
        
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1)
                msg_count += 1
                
                # Parse
                content = msg.strip()
                
                if content == "[]" or content == "":
                    empty_count += 1
                    # Only log every 10th empty
                    if empty_count % 10 == 1:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Empty messages: {empty_count}")
                    continue
                
                # NON-EMPTY MESSAGE - this is what we want!
                print(f"\n{'🔥' * 20}")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] MESSAGE #{msg_count}")
                print(f"{'🔥' * 20}")
                
                try:
                    data = json.loads(content)
                    print(f"Type: {type(data).__name__}")
                    
                    if isinstance(data, list):
                        print(f"List length: {len(data)}")
                        for i, item in enumerate(data):
                            print(f"\n  Item {i}:")
                            if isinstance(item, dict):
                                for k, v in item.items():
                                    print(f"    {k}: {str(v)[:80]}")
                            else:
                                print(f"    {item}")
                    elif isinstance(data, dict):
                        for k, v in data.items():
                            print(f"  {k}: {str(v)[:100]}")
                    else:
                        print(f"  {data}")
                        
                except json.JSONDecodeError:
                    print(f"Raw (not JSON): {content[:200]}")
                
                print()
                    
            except asyncio.TimeoutError:
                elapsed = int(time.time() - start_time)
                if elapsed % 10 == 0:  # Every 10 seconds
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Waiting... ({elapsed}s, {msg_count} msgs, {empty_count} empty)")

if __name__ == "__main__":
    print("WebSocket Market Channel Test")
    print("=" * 60)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")