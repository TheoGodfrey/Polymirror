#!/usr/bin/env python3
"""
Polymarket Mirror Bot (MAXIMUM OVERDRIVE - V3.2)
-----------------------------------------------
✅ FIXED: Invalid Amounts / Precision 400 Errors
✅ FIXED: Balance Rejection (Added Auto-Scaling)
✅ ACTIVITY STREAM: Low-latency trade detection
"""

import os
import sys
import time
import asyncio
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from dotenv import load_dotenv

# Fast JSON
try:
    import orjson
    def json_loads(s): return orjson.loads(s)
except ImportError:
    import json
    def json_loads(s): return json.loads(s)

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    import requests

load_dotenv()

# --- CONFIGURATION ---
BUY = "BUY"
CHAIN_ID = 137
POLYMARKET_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "").lower()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

# Multiplier & Risk
POSITION_MULTIPLIER = float(os.getenv("POSITION_MULTIPLIER", "2.0"))
MAX_CAPITAL = float(os.getenv("MAX_CAPITAL_TO_USE", "5000"))
MAX_BUY_PRICE = 0.99
MAX_UPPER_SLIP = 0.05
MIN_TRADE_USD = 1.05
LOOP_DELAY = 0.5 

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# CLOB Client Imports
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, AssetType
except ImportError:
    logger.error("❌ CRITICAL: py_clob_client missing. Run: pip install py-clob-client")
    sys.exit(1)

class OverdriveBot:
    __slots__ = (
        'target', 'client', 'my_addr', 'processed_activities', 
        'my_cash', 'total_spent', 'last_hb', 'executor', 
        'trades_fired', 'trades_filled'
    )
    
    def __init__(self):
        self.target = TARGET_ADDRESS
        self.processed_activities = set() 
        self.my_cash = 0.0
        self.total_spent = 0.0
        self.last_hb = 0.0
        self.trades_fired = 0
        self.trades_filled = 0
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.my_addr = FUNDER_ADDRESS.lower()
        
        # Initialize CLOB
        try:
            self.client = ClobClient(
                POLYMARKET_API, 
                key=PRIVATE_KEY, 
                chain_id=CHAIN_ID, 
                signature_type=SIGNATURE_TYPE, 
                funder=FUNDER_ADDRESS
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            logger.info(f"✅ Bot Ready: {self.my_addr[:10]}...")
        except Exception as e:
            logger.error(f"❌ Init failed: {e}")
            sys.exit(1)

    def get_balance(self) -> float:
        try:
            res = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return float(res.get("balance", 0)) / 1_000_000
        except:
            return 0.0

    def fire_order(self, tid: str, target_usd: float, limit_price: float, name: str):
        """Precision-safe order execution with auto-scaling."""
        try:
            # Refresh local balance for the check
            self.my_cash = self.get_balance()
            
            # Calculate desired spend and scale to available funds
            raw_spend = target_usd * POSITION_MULTIPLIER
            remaining_cap = MAX_CAPITAL - self.total_spent
            safe_spend = min(raw_spend, remaining_cap, self.my_cash - 0.05)
            
            if safe_spend < MIN_TRADE_USD:
                logger.error(f"❌ SKIPPED: Spend ${safe_spend:.2f} too low (Min ${MIN_TRADE_USD})")
                return

            # FORCE PRECISION: Maker (USDC) 2 decimals, Taker (Shares) 4 decimals
            maker_amt = Decimal(str(safe_spend)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            price_dec = Decimal(str(limit_price))
            shares = (maker_amt / price_dec).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            
            logger.info(f"🚀 FIRING: {shares} shares @ ${price_dec} | {name[:25]}")

            order = OrderArgs(
                token_id=tid, 
                price=float(price_dec), 
                size=float(shares), 
                side=BUY
            )
            
            signed = self.client.create_order(order)
            resp = self.client.post_order(signed, orderType=OrderType.FOK)
            
            if resp and resp.get("success"):
                self.trades_filled += 1
                self.total_spent += float(maker_amt)
                logger.info(f"💰 SUCCESS: Bought {shares} shares for ${maker_amt}")
            else:
                logger.error(f"❌ REJECTED: {resp.get('errorMsg') or resp}")

        except Exception as e:
            logger.error(f"💥 ORDER ERROR: {e}")

    async def poll_activity(self):
        """Polls the activity endpoint for the fastest trade detection."""
        url = f"{DATA_API}/activity?user={self.target}&limit=10&type=TRADE"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=5) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
                    
                    # Initial sync: fill the set but don't trade on old history
                    if not self.processed_activities:
                        for a in data:
                            self.processed_activities.add(a.get("id") or a.get("transactionHash"))
                        logger.info(f"📍 Synchronized with {self.target[:10]} activity stream.")
                        return

                    # Process new trades (oldest to newest in this slice)
                    for act in reversed(data):
                        act_id = act.get("id") or act.get("transactionHash")
                        if act_id in self.processed_activities or act.get("side") != "BUY":
                            continue
                        
                        self.processed_activities.add(act_id)
                        
                        tid = act.get("asset")
                        name = act.get("title", "Unknown Market")
                        size = float(act.get("size", 0))
                        price = float(act.get("price", 0))
                        
                        target_usd = size * price
                        limit_price = min(price + MAX_UPPER_SLIP, MAX_BUY_PRICE)

                        logger.info(f"🔔 NEW TRADE: Target bought {size} @ ${price} in {name[:20]}")
                        self.trades_fired += 1
                        self.executor.submit(self.fire_order, tid, target_usd, limit_price, name)

            except Exception as e:
                logger.debug(f"Polling error: {e}")

    async def run_async(self):
        logger.info("⚡ OVERDRIVE V3.2 ACTIVE")
        while True:
            await self.poll_activity()
            
            # Heartbeat & Maintenance
            if time.time() - self.last_hb > 10:
                self.my_cash = self.get_balance()
                logger.info(f"💓 Bal: ${self.my_cash:.2f} | Spent: ${self.total_spent:.2f} | Fills: {self.trades_filled}/{self.trades_fired}")
                self.last_hb = time.time()
                
                # Prevent memory bloat from activity IDs
                if len(self.processed_activities) > 1000:
                    self.processed_activities = set(list(self.processed_activities)[-500:])

            await asyncio.sleep(LOOP_DELAY)

    def run(self):
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            logger.info("⏹️ Stopped.")

if __name__ == "__main__":
    if not PRIVATE_KEY or not TARGET_ADDRESS:
        print("❌ Missing environment variables.")
        sys.exit(1)
    
    bot = OverdriveBot()
    bot.run()