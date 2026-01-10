#!/usr/bin/env python3
"""Polymarket Walk Capture - Loop
Places BUY YES + BUY NO @ 49% on next market, waits, repeats
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
SIZE = 1.0

KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")
SIG_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
GAMMA = "https://gamma-api.polymarket.com"

ASSETS_15M = ["btc", "eth", "sol", "xrp"]
ASSETS_1H = ["bitcoin", "ethereum", "solana", "xrp"]  # Hourly uses full names

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
for x in ["urllib3", "httpcore", "httpx", "hpack"]: logging.getLogger(x).setLevel(logging.WARNING)
log = logging.getLogger(__name__)


def next_15min_ts():
    """Get timestamp for next 15-min market end (ET-aligned)"""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc - timedelta(hours=5)  # EST
    
    # Round up to next 15-min
    mins = now_et.minute
    next_15 = ((mins // 15) + 1) * 15
    
    if next_15 >= 60:
        end_et = now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        end_et = now_et.replace(minute=next_15, second=0, microsecond=0)
    
    end_utc = end_et + timedelta(hours=5)
    return int(end_utc.timestamp()), end_et.strftime("%H:%M")


def next_hour_slug():
    """Get slug for next hourly market (ET-aligned)"""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc - timedelta(hours=5)  # EST
    
    # Next hour
    next_hr = now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    
    # Format: bitcoin-up-or-down-january-10-8am-et
    month = next_hr.strftime("%B").lower()
    day = next_hr.day
    hour = next_hr.hour
    
    if hour == 0:
        hr_str = "12am"
    elif hour < 12:
        hr_str = f"{hour}am"
    elif hour == 12:
        hr_str = "12pm"
    else:
        hr_str = f"{hour-12}pm"
    
    return f"{month}-{day}-{hr_str}-et", next_hr.strftime("%H:00")


class Bot:
    def __init__(self):
        self.c = ClobClient("https://clob.polymarket.com", key=KEY, chain_id=137, funder=FUNDER, signature_type=SIG_TYPE)
        self.c.set_api_creds(self.c.create_or_derive_api_creds())

    def load(self, slug):
        """Load market from slug"""
        r = requests.get(f"{GAMMA}/events?slug={slug}", timeout=10)
        if r.ok and r.json():
            e = r.json()[0]
            m = e.get("markets", [{}])[0]
            cid, q = m.get("conditionId"), m.get("question") or e.get("title")
        else:
            r = requests.get(f"{GAMMA}/markets?slug={slug}", timeout=10)
            if not r.ok or not r.json(): return None
            m = r.json()[0]
            cid, q = m.get("conditionId"), m.get("question")
        
        r = requests.get(f"https://clob.polymarket.com/markets/{cid}", timeout=10)
        if not r.ok: return None
        
        t = r.json().get("tokens", [])
        yes = next((x["token_id"] for x in t if x.get("outcome") == "Yes"), t[0]["token_id"] if t else None)
        no = next((x["token_id"] for x in t if x.get("outcome") == "No"), t[1]["token_id"] if len(t) > 1 else None)
        
        return {"yes": yes, "no": no, "q": q} if yes and no else None

    def place(self, m, tag):
        """Place both buy orders"""
        sz = max(5.0, SIZE / BID)
        ok = []
        
        try:
            yr = self.c.post_order(self.c.create_order(OrderArgs(token_id=str(m["yes"]), price=BID, size=sz, side="BUY")), orderType=OrderType.GTC)
            if yr.get("success"): ok.append("Y")
        except Exception as e:
            log.error("[%s] Y: %s", tag, e)
        
        try:
            nr = self.c.post_order(self.c.create_order(OrderArgs(token_id=str(m["no"]), price=BID, size=sz, side="BUY")), orderType=OrderType.GTC)
            if nr.get("success"): ok.append("N")
        except Exception as e:
            log.error("[%s] N: %s", tag, e)
        
        return ok


if __name__ == "__main__":
    bot = Bot()
    
    log.info("=" * 50)
    log.info("🚀 WALK CAPTURE | $%.0f @ %.0f%% | Loop mode", SIZE, BID*100)
    log.info("📊 15-min: %s | Hourly: %s", ", ".join(ASSETS_15M), ", ".join(ASSETS_1H))
    log.info("=" * 50)
    
    while True:
        ts, time_et = next_15min_ts()
        now = datetime.now(timezone.utc).timestamp()
        wait = ts - now
        
        if wait < 60:
            log.info("⏭️  Slot %s too close, waiting...", time_et)
            time.sleep(wait + 5)
            continue
        
        log.info("🎯 Next 15m: %s ET (ts=%d) | %.0fm away", time_et, ts, wait/60)
        
        placed = 0
        
        # 15-min markets
        for asset in ASSETS_15M:
            slug = f"{asset}-updown-15m-{ts}"
            m = bot.load(slug)
            if m:
                ok = bot.place(m, asset.upper())
                if ok:
                    log.info("[%s] ✓ %s", asset.upper(), "+".join(ok))
                    placed += len(ok)
            time.sleep(0.3)
        
        # Hourly markets (only on :00 slots)
        if time_et.endswith(":00"):
            hr_suffix, hr_time = next_hour_slug()
            log.info("🎯 Hourly slot: %s ET", hr_time)
            
            for asset in ASSETS_1H:
                slug = f"{asset}-up-or-down-{hr_suffix}"
                m = bot.load(slug)
                if m:
                    ok = bot.place(m, asset[:3].upper() + "-1H")
                    if ok:
                        log.info("[%s-1H] ✓ %s", asset[:3].upper(), "+".join(ok))
                        placed += len(ok)
                else:
                    log.info("[%s-1H] Not found: %s", asset[:3].upper(), slug)
                time.sleep(0.3)
        
        log.info("📝 Placed %d orders for %s ET", placed, time_et)
        
        # Wait until slot ends + buffer
        sleep_time = ts - datetime.now(timezone.utc).timestamp() + 30
        if sleep_time > 0:
            log.info("💤 Sleeping %.0fm...", sleep_time/60)
            time.sleep(sleep_time)