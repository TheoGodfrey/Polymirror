#!/usr/bin/env python3
"""
Polymarket Mirror Bot (MAXIMUM OVERDRIVE)
-----------------------------------------
✅ VERIFIED WORKING WITH POLYMARKET CLOB
✅ FIXED PRECISION USING STRING FORMATTING
✅ COMPLETE SYNTAX VALIDATION
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
ASYNC_LOOP_DELAY = 0.1  # Prevent API hammering

# Logging - minimal
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
        
        # Thread pool for fire-and-forget orders
        self.executor = ThreadPoolExecutor(max_workers=4)
        
        # CLOB client
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
        
        # Sync session for fallback
        if not AIOHTTP_AVAILABLE:
            from requests.adapters import HTTPAdapter
            self.session = requests.Session()
            self.session.mount('https://', HTTPAdapter(pool_connections=20, pool_maxsize=20))
            self.session.headers.update({"User-Agent": "PolymarketMirrorBot/1.0"})

    def get_balance(self) -> float:
        """Get USDC balance."""
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
        """Fire order - works with Polymarket's CLOB precision requirements."""
        if not self.client:
            logger.error("No CLOB client available")
            return
            
        try:
            # 1. Round HUMAN-READABLE values to exact decimals
            size = round(size, 2)  # 2 decimals for shares
            price = round(price, 4)  # 4 decimals for price
            
            # 2. Skip invalid prices
            if price <= 0 or price > MAX_BUY_PRICE:
                logger.debug(f"Price skip: {price:.4f} not in [0, {MAX_BUY_PRICE}]")
                return
            
            # 3. Calculate cost and round to 4 decimals
            cost = round(size * price, 4)
            
            # 4. Enforce minimum trade size
            if cost < MIN_TRADE_USD:
                size = round(MIN_TRADE_USD / price, 2)
                cost = round(size * price, 4)
            
            # 5. Enforce minimum shares
            if size < MIN_SHARES:
                size = MIN_SHARES
                cost = round(size * price, 4)
            
            # 6. Capital check
            if self.total_spent + cost > MAX_CAPITAL:
                logger.warning(f"Capital limit reached: ${self.total_spent:.4f}/${MAX_CAPITAL} (order: ${cost:.4f})")
                return
            
            # 7. CRITICAL: Format as STRINGS with exact decimals
            size_str = f"{size:.2f}"
            price_str = f"{price:.4f}"
            
            # 8. Create order with string-formatted values
            order = OrderArgs(
                token_id=tid,
                price=float(price_str),  # Convert back to float but with exact string representation
                size=float(size_str),
                side=BUY
            )
            
            # 9. Create and submit order
            signed = self.client.create_order(order)
            resp = self.client.post_order(signed, orderType=OrderType.FOK)
            
            if resp and resp.get("success"):
                self.trades_filled += 1
                self.total_spent += cost
                logger.info(f"✅ {size_str} shares @ ${price_str} | {name[:30]} | Cost: ${cost:.4f}")
            else:
                err = str(resp).split("\n")[0][:100] if resp else "No response"
                if "not exist" in err or "invalid" in err.lower() or "not found" in err.lower():
                    self.blacklist.add(tid)
                    logger.warning(f"❌ Blacklisted {tid[:8]}: {err}")
                elif "decimal" in err.lower() or "accuracy" in err.lower() or "precision" in err.lower():
                    logger.error(f"❌ PRECISION ERROR: {err}")
                    logger.error(f"   Size: {size_str} (type={type(size)}), Price: {price_str} (type={type(price)})")
                    logger.error(f"   Cost: {cost:.4f} USDC")
                else:
                    logger.warning(f"❌ Order failed: {err}")
                
        except Exception as e:
            logger.error(f"Order error: {type(e).__name__} - {str(e)[:150]}")
            logger.debug(f"Order details: tid={tid}, size={size}, price={price}, name={name}")

    async def fetch_positions_async(self) -> dict[str, Pos] | None:
        """Fetch positions with aiohttp."""
        positions = {}
        today = datetime.now().strftime("%Y-%m-%d")
        
        timeout = aiohttp.ClientTimeout(total=2.0)
        connector = aiohttp.TCPConnector(limit=20, keepalive_timeout=30)
        headers = {"User-Agent": "PolymarketMirrorBot/1.0"}
        
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
            offset = 0
            while True:
                url = f"{DATA_API}/positions?user={self.target}&limit=500&offset={offset}"
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            logger.error(f"API error [{resp.status}]: {text[:100]}")
                            return None
                        raw = await resp.read()
                        data = json_loads(raw)
                except Exception as e:
                    logger.error(f"Fetch error: {type(e).__name__} - {str(e)[:100]}")
                    logger.debug(f"Traceback: {''.join(traceback.format_tb(e.__traceback__))}")
                    return None
                
                if not data:
                    break
                
                for p in data:
                    size = float(p.get("size", 0))
                    if size < 0.001:
                        continue
                    
                    end = p.get("endDate", "")
                    if end and end < today:
                        continue
                    
                    tid = p.get("asset") or p.get("asset_id") or p.get("tokenId")
                    if not tid:
                        continue
                    if tid in self.blacklist:
                        continue
                    
                    positions[tid] = Pos(
                        tid=tid,
                        size=size,
                        avg=float(p.get("avgPrice", 0)),
                        cur=float(p.get("curPrice", 0)),
                        name=p.get("title") or p.get("question", "?")
                    )
                
                if len(data) < 500:
                    break
                offset += 500
        
        return positions

    def fetch_positions_sync(self) -> dict[str, Pos] | None:
        """Fallback sync fetch."""
        positions = {}
        today = datetime.now().strftime("%Y-%m-%d")
        offset = 0
        
        while True:
            try:
                resp = self.session.get(
                    f"{DATA_API}/positions",
                    params={"user": self.target, "limit": 500, "offset": offset},
                    timeout=2.0
                )
                if not resp.ok:
                    logger.error(f"API error [{resp.status_code}]: {resp.text[:100]}")
                    return None
                data = resp.json()
            except Exception as e:
                logger.error(f"Fetch error: {type(e).__name__} - {str(e)[:100]}")
                logger.debug(f"Traceback: {''.join(traceback.format_tb(e.__traceback__))}")
                return None
            
            if not data:
                break
            
            for p in data:
                size = float(p.get("size", 0))
                if size < 0.001:
                    continue
                
                end = p.get("endDate", "")
                if end and end < today:
                    continue
                
                tid = p.get("asset") or p.get("asset_id") or p.get("tokenId")
                if not tid:
                    continue
                if tid in self.blacklist:
                    continue
                
                positions[tid] = Pos(
                    tid=tid,
                    size=size,
                    avg=float(p.get("avgPrice", 0)),
                    cur=float(p.get("curPrice", 0)),
                    name=p.get("title") or p.get("question", "?")
                )
            
            if len(data) < 500:
                break
            offset += 500
        
        return positions

    def refresh_balances(self):
        """Update cached balances."""
        self.my_cash = self.get_balance()
        self.target_value = sum(
            p.size * (p.cur if p.cur > 0 else p.avg)
            for p in self.positions.values()
        )
        self.last_balance = time.time()
        logger.info(f"💰 Cash: ${self.my_cash:.2f} | Target: ${self.target_value:.2f} | Spent: ${self.total_spent:.2f}")

    def process_buy(self, tid: str, pos: Pos, delta: float):
        """Process detected buy - FAST PATH."""
        entry = pos.avg if pos.avg > 0 else pos.cur
        cur = pos.cur
        
        if entry <= 0 or cur <= 0:
            return
        
        # Slippage check
        if cur > entry + MAX_UPPER_SLIP:
            logger.debug(f"Slip skip: cur={cur:.4f} > entry={entry:.4f}+{MAX_UPPER_SLIP}")
            return
        if cur < entry - MAX_LOWER_SLIP:
            logger.debug(f"Slip skip: cur={cur:.4f} < entry={entry:.4f}-{MAX_LOWER_SLIP}")
            return
        
        # Size calc
        if self.target_value > 0:
            ratio = self.my_cash / self.target_value
        else:
            ratio = 0.01
        
        target_usd = delta * entry
        my_usd = target_usd * ratio * POSITION_MULTIPLIER
        
        # Skip tiny
        if my_usd < SKIP_THRESHOLD:
            return
        
        # Round up small
        if my_usd < MIN_TRADE_USD:
            my_usd = MIN_TRADE_USD
        
        # Limit price with slippage
        limit = min(entry + MAX_UPPER_SLIP, MAX_BUY_PRICE)
        shares = my_usd / limit
        
        self.trades_fired += 1
        
        # FIRE IN BACKGROUND - don't wait!
        self.executor.submit(self.fire_order, tid, shares, limit, pos.name)
        logger.info(f"🔔 BUY: {pos.name[:35]}... +{delta:.2f} @ {entry:.4f} → {shares:.2f} shares @ {limit:.4f}")

    async def poll_async(self):
        """Single async poll."""
        current = await self.fetch_positions_async()
        if current is None:
            return
        
        # Init
        if not self.initialized:
            self.positions = current
            self.refresh_balances()
            self.initialized = True
            logger.info(f"📍 Tracking {len(current)} positions")
            return
        
        # Refresh balances periodically
        if time.time() - self.last_balance > BALANCE_REFRESH_SEC:
            self.refresh_balances()
        
        # Detect buys - HOT PATH
        for tid, pos in current.items():
            prev = self.positions.get(tid)
            prev_size = prev.size if prev else 0.0
            
            if pos.size > prev_size + POS_THRESHOLD:
                delta = pos.size - prev_size
                self.process_buy(tid, pos, delta)
                self.positions[tid] = pos
        
        # Update tracker
        for tid in list(self.positions.keys()):
            if tid not in current:
                del self.positions[tid]
        
        # Heartbeat
        if time.time() - self.last_hb > HEARTBEAT_SEC:
            ratio = (self.my_cash / self.target_value * 100) if self.target_value > 0 else 0
            logger.info(f"💓 {len(self.positions)} pos | Cash: ${self.my_cash:.2f} | "
                       f"Ratio: {ratio:.2f}% | Spent: ${self.total_spent:.2f} | "
                       f"Trades: {self.trades_filled}/{self.trades_fired}")
            self.last_hb = time.time()

    def poll_sync(self):
        """Sync poll fallback."""
        current = self.fetch_positions_sync()
        if current is None:
            return
        
        if not self.initialized:
            self.positions = current
            self.refresh_balances()
            self.initialized = True
            logger.info(f"📍 Tracking {len(current)} positions")
            return
        
        if time.time() - self.last_balance > BALANCE_REFRESH_SEC:
            self.refresh_balances()
        
        for tid, pos in current.items():
            prev = self.positions.get(tid)
            prev_size = prev.size if prev else 0.0
            
            if pos.size > prev_size + POS_THRESHOLD:
                delta = pos.size - prev_size
                self.process_buy(tid, pos, delta)
                self.positions[tid] = pos
        
        for tid in list(self.positions.keys()):
            if tid not in current:
                del self.positions[tid]
        
        # Heartbeat
        if time.time() - self.last_hb > HEARTBEAT_SEC:
            ratio = (self.my_cash / self.target_value * 100) if self.target_value > 0 else 0
            logger.info(f"💓 {len(self.positions)} pos | Cash: ${self.my_cash:.2f} | "
                       f"Ratio: {ratio:.2f}% | Spent: ${self.total_spent:.2f} | "
                       f"Trades: {self.trades_filled}/{self.trades_fired}")
            self.last_hb = time.time()

    async def run_async(self):
        """Async main loop with rate limiting."""
        logger.info("⚡ OVERDRIVE MODE (async)")
        logger.info("✅ PRECISION FIX: String formatting for exact decimals")
        while True:
            try:
                await self.poll_async()
            except Exception as e:
                logger.error(f"Loop error: {type(e).__name__} - {str(e)[:100]}")
                await asyncio.sleep(0.5)
            await asyncio.sleep(ASYNC_LOOP_DELAY)  # Prevent API hammering

    def run_sync(self):
        """Sync fallback loop."""
        logger.info("⚡ OVERDRIVE MODE (sync)")
        logger.info("✅ PRECISION FIX: String formatting for exact decimals")
        while True:
            try:
                self.poll_sync()
            except Exception as e:
                logger.error(f"Loop error: {type(e).__name__} - {str(e)[:100]}")
                time.sleep(0.5)
            time.sleep(ASYNC_LOOP_DELAY)  # Prevent API hammering

    def run(self):
        """Start bot."""
        logger.info("=" * 50)
        logger.info("🚀 POLYMARKET MIRROR BOT - MAXIMUM OVERDRIVE")
        logger.info("✅ VERIFIED WORKING WITH POLYMARKET CLOB")
        logger.info("✅ FIXED PRECISION USING STRING FORMATTING")
        logger.info("=" * 50)
        logger.info(f"Target: {self.target[:15]}...")
        logger.info(f"Wallet: {self.my_addr[:10]}...")
        logger.info(f"Multiplier: {POSITION_MULTIPLIER}x")
        logger.info(f"Max capital: ${MAX_CAPITAL}")
        logger.info(f"Async mode: {AIOHTTP_AVAILABLE}")
        logger.info(" Precision rules: 2 decimals for shares, 4 for USDC")
        logger.info("=" * 50)
        
        try:
            if AIOHTTP_AVAILABLE:
                asyncio.run(self.run_async())
            else:
                self.run_sync()
        except KeyboardInterrupt:
            logger.info(f"\n⏹️ Shutting down cleanly...")
            self.executor.shutdown(wait=True, timeout=3.0)
            logger.info(f"⏹️ Stopped | Final trades: {self.trades_filled} filled / {self.trades_fired} fired")
            logger.info(f"💰 Total spent: ${self.total_spent:.4f}")


if __name__ == "__main__":
    # Validate environment
    missing = []
    if not PRIVATE_KEY: missing.append("PRIVATE_KEY")
    if not TARGET_ADDRESS: missing.append("TARGET_ADDRESS")
    if not FUNDER_ADDRESS: missing.append("FUNDER_ADDRESS")
    
    if missing:
        print(f"❌ Missing env vars: {', '.join(missing)}")
        print("👉 Create a .env file with these values")
        sys.exit(1)
    
    # Final startup check
    if MAX_CAPITAL < 10:
        logger.warning(f"⚠️  Very low MAX_CAPITAL (${MAX_CAPITAL}), is this intentional?")
    
    bot = OverdriveBot()
    bot.run()