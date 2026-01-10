#!/usr/bin/env python3
"""
Polymarket Mirror Bot (MAXIMUM OVERDRIVE - V2)
-----------------------------------------
✅ HANDLES LARGE POSITION LISTS (250+)
✅ ROBUST PAGINATION & TIMEOUTS
✅ FIXED PRECISION FOR CLOB
"""

import os
import sys
import time
import math
import asyncio
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from dotenv import load_dotenv

# Fast JSON
try:
    import orjson
    def json_loads(s): return orjson.loads(s)
except ImportError:
    import json
    def json_loads(s): return json.loads(s)

# Async HTTP
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

# Multiplier
POSITION_MULTIPLIER = float(os.getenv("POSITION_MULTIPLIER", "2.0"))

# Risk
MAX_CAPITAL = float(os.getenv("MAX_CAPITAL_TO_USE", "5000"))
MAX_BUY_PRICE = 0.99
MAX_UPPER_SLIP = 0.05
MAX_LOWER_SLIP = 0.15

# Thresholds
MIN_TRADE_USD = 1.05
MIN_SHARES = 5.0
SKIP_THRESHOLD = 0.55
POS_THRESHOLD = 0.0001

# Speed
BALANCE_REFRESH_SEC = 60
HEARTBEAT_SEC = 10
ASYNC_LOOP_DELAY = 0.5  # Increased slightly to handle larger data processing

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("aiohttp").setLevel(logging.ERROR)

# CLOB
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, AssetType
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False


@dataclass(slots=True)
class Pos:
    tid: str
    size: float
    avg: float
    cur: float
    name: str


class OverdriveBot:
    __slots__ = (
        'target', 'client', 'my_addr', 'positions', 'initialized',
        'target_value', 'my_cash', 'total_spent', 'last_balance',
        'last_hb', 'blacklist', 'executor', 'session', 'trades_fired', 'trades_filled'
    )
    
    def __init__(self):
        self.target = TARGET_ADDRESS
        self.positions: dict[str, Pos] = {}
        self.initialized = False
        self.target_value = 0.0
        self.my_cash = 0.0
        self.total_spent = 0.0
        self.last_balance = 0.0
        self.last_hb = 0.0
        self.blacklist: set[str] = set()
        self.trades_fired = 0
        self.trades_filled = 0
        
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.client = None
        self.my_addr = FUNDER_ADDRESS.lower()
        
        if CLOB_AVAILABLE and PRIVATE_KEY:
            try:
                self.client = ClobClient(
                    POLYMARKET_API, 
                    key=PRIVATE_KEY, 
                    chain_id=CHAIN_ID,
                    signature_type=SIGNATURE_TYPE,
                    funder=FUNDER_ADDRESS
                )
                try:
                    credentials = self.client.create_or_derive_api_creds()
                    self.client.set_api_creds(credentials)
                except Exception as e:
                    logger.warning(f"API creds warning: {e}")
                logger.info(f"✅ Ready: {self.my_addr[:10]}...")
            except Exception as e:
                logger.error(f"❌ Init failed: {e}")
                sys.exit(1)
        
        if not AIOHTTP_AVAILABLE:
            from requests.adapters import HTTPAdapter
            self.session = requests.Session()
            self.session.mount('https://', HTTPAdapter(pool_connections=20, pool_maxsize=20))
            self.session.headers.update({"User-Agent": "PolymarketMirrorBot/1.0"})

    def get_balance(self) -> float:
        if not self.client:
            return 0.0
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self.client.get_balance_allowance(params)
            return float(result.get("balance", 0)) / 1_000_000
        except Exception as e:
            logger.error(f"Balance error: {e}")
            return 0.0

    def fire_order(self, tid: str, size: float, price: float, name: str):
        if not self.client:
            return
            
        try:
            size = round(size, 2)
            price = round(price, 4)
            
            if price <= 0 or price > MAX_BUY_PRICE:
                return
            
            cost = round(size * price, 4)
            
            if cost < MIN_TRADE_USD:
                size = round(MIN_TRADE_USD / price, 2)
                cost = round(size * price, 4)
            
            if size < MIN_SHARES:
                size = MIN_SHARES
                cost = round(size * price, 4)
            
            if self.total_spent + cost > MAX_CAPITAL:
                logger.warning(f"Capital limit reached: ${self.total_spent:.2f}/${MAX_CAPITAL}")
                return
            
            size_str = f"{size:.2f}"
            price_str = f"{price:.4f}"
            
            order = OrderArgs(
                token_id=tid,
                price=float(price_str),
                size=float(size_str),
                side=BUY
            )
            
            signed = self.client.create_order(order)
            resp = self.client.post_order(signed, orderType=OrderType.FOK)
            
            if resp and resp.get("success"):
                self.trades_filled += 1
                self.total_spent += cost
                logger.info(f"✅ {size_str} shares @ ${price_str} | {name[:30]} | Cost: ${cost:.2f}")
            else:
                err = str(resp).split("\n")[0][:100] if resp else "No response"
                if "not exist" in err or "invalid" in err.lower():
                    self.blacklist.add(tid)
                logger.warning(f"❌ Order failed: {err}")
                
        except Exception as e:
            logger.error(f"Order error: {type(e).__name__} - {str(e)[:100]}")

    async def fetch_positions_async(self) -> dict[str, Pos] | None:
        all_positions = {}
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Longer timeout for large payloads
        timeout = aiohttp.ClientTimeout(total=5.0)
        connector = aiohttp.TCPConnector(limit=10)
        headers = {"User-Agent": "PolymarketMirrorBot/1.0"}
        
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
            offset = 0
            while True:
                url = f"{DATA_API}/positions?user={self.target}&limit=100&offset={offset}"
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.error(f"API error [{resp.status}]")
                            return None
                        raw = await resp.read()
                        data = json_loads(raw)
                except Exception as e:
                    logger.error(f"Fetch error: {type(e).__name__}")
                    return None
                
                if not data or not isinstance(data, list):
                    break
                
                for p in data:
                    size = float(p.get("size", 0))
                    if size < 0.001: continue
                    
                    end = p.get("endDate", "")
                    if end and end < today: continue
                    
                    tid = p.get("asset") or p.get("asset_id") or p.get("tokenId")
                    if not tid or tid in self.blacklist: continue
                    
                    all_positions[tid] = Pos(
                        tid=tid,
                        size=size,
                        avg=float(p.get("avgPrice", 0)),
                        cur=float(p.get("curPrice", 0)),
                        name=p.get("title") or p.get("question", "?")
                    )
                
                if len(data) < 100:
                    break
                offset += 100
        
        return all_positions

    def refresh_balances(self):
        self.my_cash = self.get_balance()
        # Sum target value from current position set
        self.target_value = sum(
            p.size * (p.cur if p.cur > 0 else p.avg)
            for p in self.positions.values()
        )
        self.last_balance = time.time()
        logger.info(f"💰 Cash: ${self.my_cash:.2f} | Target Port: ${self.target_value:.2f}")

    def process_buy(self, tid: str, pos: Pos, delta: float):
        entry = pos.avg if pos.avg > 0 else pos.cur
        cur = pos.cur
        if entry <= 0 or cur <= 0: return
        
        # Slippage checks
        if cur > entry + MAX_UPPER_SLIP: return
        if cur < entry - MAX_LOWER_SLIP: return
        
        # Allocation logic
        ratio = (self.my_cash / self.target_value) if self.target_value > 0 else 0.05
        target_usd = delta * entry
        my_usd = target_usd * ratio * POSITION_MULTIPLIER
        
        if my_usd < SKIP_THRESHOLD: return
        if my_usd < MIN_TRADE_USD: my_usd = MIN_TRADE_USD
        
        limit = min(entry + MAX_UPPER_SLIP, MAX_BUY_PRICE)
        shares = my_usd / limit
        
        self.trades_fired += 1
        self.executor.submit(self.fire_order, tid, shares, limit, pos.name)
        logger.info(f"🔔 DETECTED BUY: {pos.name[:30]}... +{delta:.2f} shares")

    async def poll_async(self):
        current = await self.fetch_positions_async()
        if current is None: return
        
        if not self.initialized:
            self.positions = current
            self.refresh_balances()
            self.initialized = True
            logger.info(f"📍 Initialized: Tracking {len(current)} positions")
            return
        
        if time.time() - self.last_balance > BALANCE_REFRESH_SEC:
            self.refresh_balances()
        
        # Compare current state to cached state
        for tid, pos in current.items():
            prev = self.positions.get(tid)
            prev_size = prev.size if prev else 0.0
            
            # Detect size increase
            if pos.size > prev_size + POS_THRESHOLD:
                delta = pos.size - prev_size
                self.process_buy(tid, pos, delta)
        
        # Sync the cache
        self.positions = current
        
        if time.time() - self.last_hb > HEARTBEAT_SEC:
            logger.info(f"💓 Active: {len(self.positions)} pos | Trades: {self.trades_filled}/{self.trades_fired}")
            self.last_hb = time.time()

    async def run_async(self):
        logger.info("⚡ OVERDRIVE STARTING...")
        while True:
            try:
                await self.poll_async()
            except Exception as e:
                logger.error(f"Loop error: {e}")
                await asyncio.sleep(1)
            await asyncio.sleep(ASYNC_LOOP_DELAY)

    def run(self):
        try:
            if AIOHTTP_AVAILABLE:
                asyncio.run(self.run_async())
            else:
                logger.error("aiohttp required for pagination efficiency.")
        except KeyboardInterrupt:
            logger.info("⏹️ Stopped.")

if __name__ == "__main__":
    if not PRIVATE_KEY or not TARGET_ADDRESS:
        print("❌ Missing env vars.")
        sys.exit(1)
    
    bot = OverdriveBot()
    bot.run()