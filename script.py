#!/usr/bin/env python3
"""Polymarket Walk Capture - Fire & Forget
Place BUY YES @ 49% + BUY NO @ 49%
If both fill: guaranteed 2% profit at resolution
If one fills: 50/50 gamble
"""

import os
import time
import requests
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

load_dotenv()

BID = 0.49
SIZE = 5.0
HOURS_AHEAD = 4  # How far ahead to place orders

KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")
SIG_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
GAMMA = "https://gamma-api.polymarket.com"

ASSETS = ["btc", "eth", "sol", "xrp"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
for x in ["urllib3", "httpcore", "httpx", "hpack"]: logging.getLogger(x).setLevel(logging.WARNING)
log = logging.getLogger(__name__)


def gen_slugs(hours=4):
    """Generate 15-min market slugs (ET-aligned)"""
    # Markets are aligned to ET (Eastern Time)
    et = timezone(timedelta(hours=-5))  # EST (adjust to -4 for EDT)
    now_et = datetime.now(et)
    
    # Round up to next 15-min boundary in ET
    mins = (now_et.minute // 15 + 1) * 15
    if mins >= 60:
        start = now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        start = now_et.replace(minute=mins, second=0, microsecond=0)
    
    slugs = []
    for i in range(hours * 4):
        end_time = start + timedelta(minutes=15 * i)
        ts = int(end_time.timestamp())
        for asset in ASSETS:
            slugs.append(f"{asset}-updown-15m-{ts}")
    
    log.info("First slot: %s ET (ts=%d)", start.strftime("%H:%M"), int(start.timestamp()))
    return slugs


class Bot:
    def __init__(self):
        self.c = ClobClient("https://clob.polymarket.com", key=KEY, chain_id=137, funder=FUNDER, signature_type=SIG_TYPE)
        self.c.set_api_creds(self.c.create_or_derive_api_creds())

    def load(self, slug):
        """Load market from slug"""
        slug = slug.strip().rstrip('/').split('/')[-1]
        
        r = requests.get(f"{GAMMA}/events?slug={slug}", timeout=10)
        if r.ok and r.json():
            e = r.json()[0]
            m = e.get("markets", [{}])[0]
            cid, q, end = m.get("conditionId"), m.get("question") or e.get("title"), m.get("endDate") or e.get("endDate")
        else:
            r = requests.get(f"{GAMMA}/markets?slug={slug}", timeout=10)
            if not r.ok or not r.json(): return None
            m = r.json()[0]
            cid, q, end = m.get("conditionId"), m.get("question"), m.get("endDate")
        
        if isinstance(end, str):
            end = int(datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp() * 1000)
        
        r = requests.get(f"https://clob.polymarket.com/markets/{cid}", timeout=10)
        if not r.ok: return None
        
        t = r.json().get("tokens", [])
        yes = next((x["token_id"] for x in t if x.get("outcome") == "Yes"), t[0]["token_id"] if t else None)
        no = next((x["token_id"] for x in t if x.get("outcome") == "No"), t[1]["token_id"] if len(t) > 1 else None)
        
        return {"yes": yes, "no": no, "end": end, "q": q} if yes and no else None

    def place(self, m):
        """Place both buy orders"""
        tag = m["q"].replace("Up or Down", "").replace("January", "").split("-")[0].strip()[:12]
        mins = int((m["end"]/1000 - datetime.now(timezone.utc).timestamp()) / 60)
        sz = max(5.0, SIZE / BID)
        
        placed = []
        
        try:
            yr = self.c.post_order(self.c.create_order(OrderArgs(token_id=str(m["yes"]), price=BID, size=sz, side="BUY")), orderType=OrderType.GTC)
            if yr.get("success"):
                placed.append("Y")
        except Exception as e:
            log.error("[%s] Y err: %s", tag, e)
        
        try:
            nr = self.c.post_order(self.c.create_order(OrderArgs(token_id=str(m["no"]), price=BID, size=sz, side="BUY")), orderType=OrderType.GTC)
            if nr.get("success"):
                placed.append("N")
        except Exception as e:
            log.error("[%s] N err: %s", tag, e)
        
        if placed:
            log.info("[%s] ✓ %s (%dm)", tag, "+".join(placed), mins)
        else:
            log.info("[%s] ✗ Failed", tag)
        
        return len(placed)


if __name__ == "__main__":
    import sys
    
    # Debug mode - show generated slugs
    if "--debug" in sys.argv:
        slugs = gen_slugs(hours=2)
        log.info("Generated %d slugs:", len(slugs))
        for s in slugs[:8]:  # First 2 slots
            log.info("  %s", s)
        exit(0)
    
    bot = Bot()
    
    # Discover markets
    log.info("🔍 Discovering...")
    
    markets = []
    seen = set()
    
    # 15-min markets
    slugs_15m = gen_slugs(hours=HOURS_AHEAD)
    log.info("Checking %d 15-min slots (%dh ahead)...", len(slugs_15m), HOURS_AHEAD)
    for slug in slugs_15m:
        m = bot.load(slug)
        if m:
            key = (m["yes"], m["no"])
            if key not in seen:
                seen.add(key)
                m["type"] = "15m"
                markets.append(m)
    
    # Hourly markets from API
    log.info("Checking hourly markets...")
    try:
        r = requests.get(f"{GAMMA}/events", params={"active": "true", "closed": "false", "limit": 200}, timeout=15)
        if r.ok:
            for e in r.json():
                s = e.get("slug", "")
                if "up-or-down" in s.lower():
                    m = bot.load(s)
                    if m:
                        key = (m["yes"], m["no"])
                        if key not in seen:
                            seen.add(key)
                            m["type"] = "1h"
                            markets.append(m)
    except Exception as ex:
        log.error("Hourly fetch failed: %s", ex)
    
    # Filter and sort
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    markets = [m for m in markets if m["end"] - now > 180000]  # 3+ min left
    markets.sort(key=lambda x: x["end"])
    
    # Count types
    count_15m = sum(1 for m in markets if m.get("type") == "15m")
    count_1h = sum(1 for m in markets if m.get("type") == "1h")
    
    log.info("=" * 50)
    log.info("🚀 WALK CAPTURE | $%.0f @ %.0f%% | %dh ahead", SIZE, BID*100, HOURS_AHEAD)
    log.info("📅 %d markets (%d×15min, %d×1h)", len(markets), count_15m, count_1h)
    log.info("💰 Capital needed: ~$%.0f (2 orders × %d markets × $%.0f)", 
             2 * len(markets) * SIZE * BID, len(markets), SIZE * BID)
    log.info("💡 Both fill = 2%% profit | One fill = 50/50")
    log.info("=" * 50)
    
    if not markets:
        log.error("No markets found")
        exit(1)
    
    # Show schedule
    log.info("📋 Schedule:")
    for m in markets[:10]:
        mins = int((m["end"]/1000 - datetime.now(timezone.utc).timestamp()) / 60)
        log.info("   [%s] %s (%dm)", m.get("type", "?"), m["q"][:35], mins)
    if len(markets) > 10:
        last_mins = int((markets[-1]["end"]/1000 - datetime.now(timezone.utc).timestamp()) / 60)
        log.info("   ... +%d more (last in %dm / %.1fh)", len(markets) - 10, last_mins, last_mins/60)
    log.info("=" * 50)
    
    # Place all orders
    total = 0
    for m in markets:
        total += bot.place(m)
        time.sleep(0.3)  # Rate limit
    
    log.info("=" * 50)
    log.info("🏁 Placed %d orders across %d markets", total, len(markets))
    log.info("💤 Orders working - check Polymarket for fills")
    log.info("=" * 50)