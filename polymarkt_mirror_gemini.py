#!/usr/bin/env python3
"""
Polymarket Mirror Bot v4 (WebSocket Edition) - FIXED
-----------------------------------------------------
- WebSocket for INSTANT trade detection
- Fallback to polling if WS disconnects
- Thread-safe execution
"""

import os
import time
import json
import logging
import asyncio
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from dataclasses import dataclass
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# --- CONSTANTS ---
BUY = "BUY"
SELL = "SELL"
CHAIN_ID = 137
POLYMARKET_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# --- CONFIGURATION ---
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "").lower()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

# Risk
MAX_CAPITAL_USAGE = Decimal(os.getenv("MAX_CAPITAL_TO_USE", "5000"))
MAX_BUY_PRICE = Decimal(os.getenv("MAX_BUY_PRICE", "0.95"))
MAX_POSITION_PCT = Decimal(os.getenv("MAX_POSITION_PCT", "0.25"))
MAX_SLIPPAGE = Decimal(os.getenv("MAX_SLIPPAGE", "0.03"))

# Execution
MIN_TRADE_USD = Decimal(os.getenv("MIN_TRADE_USD", "1.00"))
MIN_SHARES = Decimal(os.getenv("MIN_SHARES", "1.0"))

# Speed
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2.0"))
BALANCE_REFRESH_SEC = int(os.getenv("BALANCE_REFRESH_SEC", "120"))
USE_WEBSOCKET = os.getenv("USE_WEBSOCKET", "true").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Imports
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        OrderArgs, OrderType, BalanceAllowanceParams, AssetType
    )
    CLOB_AVAILABLE = True
except ImportError:
    logger.error("pip install py-clob-client")
    CLOB_AVAILABLE = False

try:
    import websockets
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    logger.warning("pip install websockets - using polling only")


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    size: Decimal
    avg_price: Decimal
    cur_price: Decimal
    market_question: str
    neg_risk: bool = False


class PolymarketMirror:
    def __init__(self):
        self.target_address = TARGET_ADDRESS
        self.my_address = FUNDER_ADDRESS.lower() if FUNDER_ADDRESS else ""
        
        # State (protected by lock)
        self._lock = threading.Lock()
        self.target_positions: dict[str, Position] = {}
        self.my_positions: dict[str, Position] = {}
        self.initialized = False
        self.total_spent = Decimal(0)
        self.ws_connected = False
        
        # Caches
        self.market_cache: dict[str, dict] = {}
        self.cached_my_cash = Decimal(0)
        self.cached_target_value = Decimal(0)
        self.last_balance_update = 0
        self.last_poll = 0
        
        # HTTP Session
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount('https://', adapter)
        
        # Thread pool
        self.executor = ThreadPoolExecutor(max_workers=4)
        
        # CLOB Client
        self.client = None
        self._init_client()
    
    def _init_client(self):
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            return
        try:
            self.client = ClobClient(
                host=POLYMARKET_API,
                chain_id=CHAIN_ID,
                key=PRIVATE_KEY,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER_ADDRESS if FUNDER_ADDRESS else None
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            logger.info(f"✅ Client ready")
        except Exception as e:
            logger.error(f"Client init failed: {e}")

    def get_cash_balance(self) -> Decimal:
        if not self.client:
            return Decimal(0)
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self.client.get_balance_allowance(params)
            if result:
                return Decimal(str(result.get("balance", 0))) / Decimal("1000000")
        except Exception as e:
            logger.debug(f"Balance error: {e}")
        return Decimal(0)

    def get_positions(self, address: str) -> dict[str, Position]:
        positions = {}
        try:
            resp = self.session.get(
                f"{DATA_API}/positions",
                params={"user": address, "limit": 500},
                timeout=5
            )
            if not resp.ok:
                return positions
            
            for p in resp.json():
                size = Decimal(str(p.get("size", 0)))
                if size < Decimal("0.001"):
                    continue
                
                token_id = p.get("asset") or p.get("asset_id")
                if not token_id:
                    continue
                
                cur_price = Decimal(str(p.get("curPrice", 0)))
                avg_price = Decimal(str(p.get("avgPrice", 0)))
                
                positions[token_id] = Position(
                    market_id=p.get("marketId", ""),
                    token_id=token_id,
                    outcome=p.get("outcome", ""),
                    size=size,
                    avg_price=avg_price,
                    cur_price=cur_price if cur_price > 0 else avg_price,
                    market_question=p.get("title", "")[:50],
                    neg_risk=p.get("negRisk", False)
                )
        except Exception as e:
            logger.error(f"Position fetch error: {e}")
        return positions

    def get_market_info(self, token_id: str) -> tuple[str, bool]:
        if token_id in self.market_cache:
            c = self.market_cache[token_id]
            return c.get("tick_size", "0.01"), c.get("neg_risk", False)
        
        try:
            resp = self.session.get(
                f"{DATA_API}/markets",
                params={"clob_token_ids": token_id},
                timeout=2
            )
            if resp.ok and resp.json():
                m = resp.json()[0]
                tick_size = m.get("minimum_tick_size", "0.01")
                neg_risk = m.get("neg_risk", False)
                self.market_cache[token_id] = {"tick_size": tick_size, "neg_risk": neg_risk}
                return tick_size, neg_risk
        except:
            pass
        return "0.01", False

    def place_order(self, token_id: str, side: str, size: Decimal, price: Decimal) -> bool:
        if not self.client:
            return False
        
        try:
            tick_size, neg_risk = self.get_market_info(token_id)
            tick = Decimal(tick_size)
            
            # FIX: Round UP for buys (higher price = more likely to fill)
            #      Round DOWN for sells (lower price = more likely to fill)
            if side == BUY:
                price_rounded = float((price / tick).quantize(Decimal("1"), ROUND_UP) * tick)
            else:
                price_rounded = float((price / tick).quantize(Decimal("1"), ROUND_DOWN) * tick)
            
            size_rounded = float(size.quantize(Decimal("0.1"), ROUND_DOWN))
            
            if size_rounded < float(MIN_SHARES):
                logger.debug(f"Size too small: {size_rounded}")
                return False
            
            t_start = time.time()
            logger.info(f"⚡ {side} {size_rounded:.1f} @ {price_rounded:.3f}")
            
            order_args = OrderArgs(
                token_id=token_id,
                price=price_rounded,
                size=size_rounded,
                side=BUY if side == BUY else SELL
            )
            
            options = {"tick_size": tick_size, "neg_risk": neg_risk}
            signed = self.client.create_order(order_args, options)
            resp = self.client.post_order(signed, order_type=OrderType.FOK)
            
            latency = (time.time() - t_start) * 1000
            
            if resp:
                success = resp.get("success") if isinstance(resp, dict) else getattr(resp, "success", False)
                if success:
                    logger.info(f"✅ Filled in {latency:.0f}ms")
                    with self._lock:
                        if side == BUY:
                            spent = Decimal(str(size_rounded * price_rounded))
                            self.total_spent += spent
                            self.cached_my_cash -= spent
                    return True
                else:
                    err = resp.get("errorMsg", str(resp)) if isinstance(resp, dict) else str(resp)
                    logger.warning(f"❌ Rejected: {err[:60]}")
            return False
            
        except Exception as e:
            logger.error(f"Order error: {e}")
            return False

    def get_dynamic_slippage(self, usd_size: Decimal) -> Decimal:
        """Dynamic slippage: more lenient for small positions, tighter for large."""
        if usd_size < Decimal("50"):
            return Decimal("0.08")   # 8% for tiny positions - just get in
        elif usd_size < Decimal("150"):
            return Decimal("0.06")   # 6%
        elif usd_size < Decimal("300"):
            return Decimal("0.05")   # 5%
        elif usd_size < Decimal("500"):
            return Decimal("0.04")   # 4%
        else:
            return Decimal("0.03")   # 3% for large positions - be careful

    def handle_trade_signal(self, token_id: str, side: str, target_size: Decimal, target_price: Decimal):
        """Process detected trade signal (runs in thread pool)."""
        # FIX: Use cur_price as fallback if target_price is 0
        if target_price <= 0:
            with self._lock:
                pos = self.target_positions.get(token_id)
                if pos:
                    target_price = pos.cur_price if pos.cur_price > 0 else pos.avg_price
            if target_price <= 0:
                logger.warning(f"No valid price for {token_id[:16]}")
                return
        
        if side == BUY:
            # Calculate our position size first to determine slippage
            with self._lock:
                my_cash = self.cached_my_cash
                target_value = self.cached_target_value
                current_spent = self.total_spent
            
            if target_value > 0:
                ratio = my_cash / target_value
            else:
                ratio = Decimal("0.01")
            
            target_usd = target_size * target_price
            my_usd = min(target_usd * ratio, my_cash * MAX_POSITION_PCT)
            
            # Dynamic slippage based on our position size
            slippage = self.get_dynamic_slippage(my_usd)
            
            # Get current price to check slippage
            with self._lock:
                pos = self.target_positions.get(token_id)
                cur_price = pos.cur_price if pos else target_price
            
            slippage_limit = target_price * (Decimal("1") + slippage)
            if cur_price > slippage_limit:
                logger.warning(f"⚠️ Slippage: {cur_price:.3f} > {slippage_limit:.3f} ({slippage*100:.0f}%)")
                return
            
            if cur_price > MAX_BUY_PRICE:
                logger.warning(f"⚠️ Price > {MAX_BUY_PRICE}")
                return
            
            if my_usd < MIN_TRADE_USD:
                logger.debug(f"Size too small: ${my_usd:.2f}")
                return
            
            # Capital check
            if current_spent + my_usd > MAX_CAPITAL_USAGE:
                logger.warning(f"Capital limit reached: ${current_spent:.2f} + ${my_usd:.2f} > ${MAX_CAPITAL_USAGE}")
                return
            
            # Use dynamic slippage for limit price
            limit_price = min(target_price * (Decimal("1") + slippage), MAX_BUY_PRICE)
            shares = my_usd / limit_price
            
            logger.info(f"   → Size: ${my_usd:.2f} | Slippage: {slippage*100:.0f}% | Limit: {limit_price:.3f}")
            self.place_order(token_id, BUY, shares, limit_price)
        
        else:  # SELL
            my_pos = self.my_positions.get(token_id)
            if not my_pos or my_pos.size <= 0:
                # Refresh and check again
                self.my_positions = self.get_positions(self.my_address)
                my_pos = self.my_positions.get(token_id)
                if not my_pos or my_pos.size <= 0:
                    logger.debug(f"We don't own {token_id[:16]}")
                    return
            
            to_sell = min(target_size, my_pos.size)
            sell_price = target_price * Decimal("0.95")
            
            if sell_price > 0:
                self.place_order(token_id, SELL, to_sell, sell_price)

    def refresh_state(self):
        with self._lock:
            self.cached_my_cash = self.get_cash_balance()
            self.cached_target_value = sum(
                p.size * (p.cur_price if p.cur_price > 0 else p.avg_price)
                for p in self.target_positions.values()
            )
            self.last_balance_update = time.time()
            logger.info(f"💰 Cash: ${self.cached_my_cash:.2f} | Target: ${self.cached_target_value:.2f}")

    def poll_once(self):
        """Single poll iteration."""
        if time.time() - self.last_balance_update > BALANCE_REFRESH_SEC:
            self.refresh_state()
        
        current = self.get_positions(self.target_address)
        
        with self._lock:
            if not self.initialized:
                self.target_positions = current
                self.my_positions = self.get_positions(self.my_address)
                self.initialized = True
                logger.info(f"📍 Tracking {len(current)} positions")
                self.last_poll = time.time()
                # Refresh after init to get proper target value
                self.refresh_state()
                return
            
            # Detect buys
            for tid, pos in current.items():
                prev = self.target_positions.get(tid)
                prev_size = prev.size if prev else Decimal(0)
                
                if pos.size > prev_size + Decimal("0.01"):
                    delta = pos.size - prev_size
                    # FIX: Use cur_price if avg_price is 0
                    entry_price = pos.avg_price if pos.avg_price > 0 else pos.cur_price
                    logger.info(f"🔔 BUY: {pos.market_question} (+{delta:.1f} @ {entry_price:.3f})")
                    self.executor.submit(
                        self.handle_trade_signal, tid, BUY, delta, entry_price
                    )
                    self.target_positions[tid] = pos
            
            # Detect sells
            for tid in list(self.target_positions.keys()):
                prev = self.target_positions[tid]
                curr = current.get(tid)
                curr_size = curr.size if curr else Decimal(0)
                
                if curr_size < prev.size - Decimal("0.01"):
                    delta = prev.size - curr_size
                    logger.info(f"🔔 SELL: {prev.market_question} (-{delta:.1f})")
                    self.executor.submit(
                        self.handle_trade_signal, tid, SELL, delta, prev.cur_price
                    )
                    
                    if curr:
                        self.target_positions[tid] = curr
                    else:
                        del self.target_positions[tid]
            
            # Track new positions
            for tid, pos in current.items():
                if tid not in self.target_positions:
                    self.target_positions[tid] = pos
            
            self.last_poll = time.time()

    async def ws_subscribe(self):
        """Subscribe to market trades via WebSocket."""
        if not WS_AVAILABLE:
            return
        
        while True:  # Reconnect loop
            try:
                with self._lock:
                    token_ids = list(self.target_positions.keys())
                
                if not token_ids:
                    await asyncio.sleep(5)
                    continue
                
                logger.info(f"🔌 Connecting WebSocket...")
                
                async with websockets.connect(
                    WS_MARKET_URL,
                    ping_interval=30,
                    ping_timeout=10
                ) as ws:
                    self.ws_connected = True
                    
                    # Subscribe to all markets in ONE message
                    subscription = {
                        "assets_ids": token_ids[:50],
                        "type": "market"
                    }
                    await ws.send(json.dumps(subscription))
                    
                    logger.info(f"✅ WS connected, watching {min(len(token_ids), 50)} markets")
                    
                    async for message in ws:
                        try:
                            # Skip empty messages
                            if not message or not message.strip():
                                continue
                            
                            data = json.loads(message)
                            
                            # Handle different response types
                            if isinstance(data, list) and len(data) > 0:
                                # Non-empty list = something happened
                                logger.debug(f"WS activity: {len(data)} events")
                                await asyncio.get_event_loop().run_in_executor(
                                    self.executor, self.poll_once
                                )
                            elif isinstance(data, dict) and data:
                                # Non-empty dict = something happened
                                event_type = data.get("event_type", "unknown")
                                logger.debug(f"WS event: {event_type}")
                                await asyncio.get_event_loop().run_in_executor(
                                    self.executor, self.poll_once
                                )
                            # Empty [] or {} = subscription confirmation, ignore
                        except json.JSONDecodeError:
                            # Not JSON, ignore
                            pass
                        except Exception as e:
                            logger.debug(f"WS parse error: {e}")
                            
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WS disconnected, reconnecting...")
            except Exception as e:
                logger.warning(f"WS error: {e}")
            
            self.ws_connected = False
            await asyncio.sleep(3)  # Wait before reconnect

    async def run_async(self):
        """Run with WebSocket + polling."""
        # Initial poll
        self.poll_once()
        
        # Start WebSocket task
        ws_task = asyncio.create_task(self.ws_subscribe())
        
        last_heartbeat = 0
        
        try:
            while True:
                # Backup polling (less frequent when WS connected)
                poll_interval = POLL_INTERVAL * 3 if self.ws_connected else POLL_INTERVAL
                
                if time.time() - self.last_poll > poll_interval:
                    await asyncio.get_event_loop().run_in_executor(
                        self.executor, self.poll_once
                    )
                
                # Heartbeat
                if time.time() - last_heartbeat > 60:
                    with self._lock:
                        pos_count = len(self.target_positions)
                        spent = self.total_spent
                    status = "WS" if self.ws_connected else "Poll"
                    logger.info(f"💓 [{status}] Positions: {pos_count} | Spent: ${spent:.2f}")
                    last_heartbeat = time.time()
                
                await asyncio.sleep(0.1)
                
        except asyncio.CancelledError:
            pass
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass

    def run(self):
        """Main entry point."""
        logger.info("=" * 50)
        logger.info("⚡ Mirror Bot v4 (Fixed)")
        logger.info(f"   Target: {self.target_address}")
        logger.info(f"   My address: {self.my_address}")
        logger.info(f"   WebSocket: {'Enabled' if USE_WEBSOCKET and WS_AVAILABLE else 'Disabled'}")
        logger.info(f"   Poll interval: {POLL_INTERVAL}s")
        logger.info(f"   Max capital: ${MAX_CAPITAL_USAGE}")
        logger.info(f"   Max slippage: {MAX_SLIPPAGE*100:.0f}%")
        logger.info("=" * 50)
        
        try:
            if USE_WEBSOCKET and WS_AVAILABLE:
                asyncio.run(self.run_async())
            else:
                # Polling only
                last_heartbeat = 0
                while True:
                    self.poll_once()
                    
                    if time.time() - last_heartbeat > 60:
                        with self._lock:
                            pos_count = len(self.target_positions)
                            spent = self.total_spent
                        logger.info(f"💓 [Poll] Positions: {pos_count} | Spent: ${spent:.2f}")
                        last_heartbeat = time.time()
                    
                    time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            self.executor.shutdown(wait=False)


if __name__ == "__main__":
    missing = []
    if not PRIVATE_KEY:
        missing.append("PRIVATE_KEY")
    if not TARGET_ADDRESS:
        missing.append("TARGET_ADDRESS")
    if not FUNDER_ADDRESS:
        missing.append("FUNDER_ADDRESS")
    
    if missing:
        print(f"❌ Missing in .env: {', '.join(missing)}")
        exit(1)
    
    bot = PolymarketMirror()
    bot.run()