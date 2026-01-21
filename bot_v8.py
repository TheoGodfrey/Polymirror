#!/usr/bin/env python3
"""
Polymarket Mirror Bot (V10 - Risk Management)
---------------------------------------------
✅ Late Entry Filter: Skip markets >95% or <5%
✅ Drawdown Protection: Pause if daily loss >15%
✅ Smart Order Delay: 3s wait + front-run detection
✅ Orderbook Depth Check: Skip illiquid markets
✅ Mirrored Position Tracking: Auto-exit when copied trader exits
✅ Whole share sizing for precision
✅ Shows trader names and P&L in logs
"""

import sys
import io

# Force UTF-8 encoding for all output streams on Windows
if sys.platform.startswith('win'):
    import os
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', write_through=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', write_through=True)

import os
import json
import logging
import asyncio
import time
import gc
import math
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import requests

# --- Dependency Checks ---
try:
    from web3 import AsyncWeb3, AsyncHTTPProvider, WebSocketProvider
    from web3.middleware import ExtraDataToPOAMiddleware
    WEB3_AVAILABLE = True
    print("✅ Web3 imported successfully")
except ImportError as e:
    print(f"CRITICAL: Failed to import Web3 - {e}")
    print("FIX: pip install web3>=7.0.0 websockets")
    exit(1)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
    print("✅ Polymarket CLOB client imported successfully")
    # Check available OrderType values
    print(f"   Available OrderTypes: {[x for x in dir(OrderType) if not x.startswith('_')]}")
except ImportError as e:
    print(f"CRITICAL: Failed to import Polymarket CLOB client - {e}")
    print("FIX: pip install py-clob-client")
    exit(1)

load_dotenv()

# --- Logging Setup ---
log_file = "bot_v10.log"
try:
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
except TypeError:
    file_handler = logging.FileHandler(log_file)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        file_handler,
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("PolyV10")

# Suppress noisy web3 logs
logging.getLogger("web3.providers.WebSocketProvider").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

# --- Strict Configuration ---
def get_env_strict(key, default=None):
    val = os.getenv(key, default)
    if not val:
        logger.critical(f"❌ STARTUP FAILED: Missing Env Var: {key}")
        raise ValueError(f"Missing required env var: {key}")
    return val.strip()

PRIVATE_KEY = get_env_strict("PRIVATE_KEY")
FUNDER_ADDRESS = get_env_strict("FUNDER_ADDRESS")

# Validate WSS - allow env override or use public endpoints
POLYGON_WSS = os.getenv("POLYGON_WS", "")
if not POLYGON_WSS:
    # Public WSS endpoints (no API key needed)
    PUBLIC_WSS_ENDPOINTS = [
        "wss://polygon-bor-rpc.publicnode.com",
        "wss://polygon.drpc.org", 
        "wss://polygon-mainnet.public.blastapi.io",
    ]
    POLYGON_WSS = PUBLIC_WSS_ENDPOINTS[0]
    logger.info(f"🌐 Using public WSS: {POLYGON_WSS}")
elif not POLYGON_WSS.startswith("wss://"):
    logger.critical(f"❌ CONFIG ERROR: POLYGON_WS must start with 'wss://'. Got: {POLYGON_WSS[:10]}...")
    raise ValueError("Invalid WSS URL Scheme")

POLYGON_HTTP = os.getenv("POLYGON_HTTP", "https://polygon-rpc.com")
CHAIN_ID = 137

# GLOBAL OVERRIDE - caps all trades regardless of whitelist.json settings
# Set these in .env to override everything
MAX_TRADE_USD = float(os.getenv("MAX_TRADE_USD", "1.10"))  # Target ~$1, actual $1-3 due to whole shares
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "10"))  # Increased to allow 3-4 trades per market

# Late Entry Filter - skip markets with extreme odds (little upside)
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.05"))  # Skip if price < 5%
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.95"))  # Skip if price > 95%

# Smart Order Timing - delay before executing to check for front-running
ORDER_DELAY_SECONDS = float(os.getenv("ORDER_DELAY_SECONDS", "3"))  # Wait before executing

# Drawdown Protection
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "15"))  # Pause if daily loss > 15%
PAUSE_DURATION_MINUTES = int(os.getenv("PAUSE_DURATION_MINUTES", "30"))  # Pause duration

# Orderbook Depth - minimum liquidity required
MIN_ORDERBOOK_DEPTH_USD = float(os.getenv("MIN_ORDERBOOK_DEPTH_USD", "50"))  # Skip if < $50 liquidity

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYMARKET_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# CTF tokens always use 6 decimals (USDC-based)
CTF_DECIMALS = 6

# Heartbeat interval in seconds
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "60"))

# Diagnostic mode - logs ALL addresses seen (set to "true" to enable)
DIAGNOSTIC_MODE = os.getenv("DIAGNOSTIC_MODE", "false").lower() == "true"

# Manual balance override for testing (set MANUAL_BALANCE=100 to simulate $100 balance)
MANUAL_BALANCE = os.getenv("MANUAL_BALANCE", "")

# Force global settings to override per-trader settings (useful when whitelist has old values)
FORCE_GLOBAL_SETTINGS = os.getenv("FORCE_GLOBAL_SETTINGS", "true").lower() == "true"

# Public Polygon WSS endpoints (fallbacks if Alchemy rate limited)
PUBLIC_WSS_ENDPOINTS = [
    "wss://polygon-bor-rpc.publicnode.com",
    "wss://polygon.drpc.org", 
    "wss://polygon-mainnet.public.blastapi.io",
]

# --- ABIs ---
CTF_ABI = [
    {"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"operator","type":"address"},{"indexed":True,"internalType":"address","name":"from","type":"address"},{"indexed":True,"internalType":"address","name":"to","type":"address"},{"indexed":False,"internalType":"uint256","name":"id","type":"uint256"},{"indexed":False,"internalType":"uint256","name":"value","type":"uint256"}],"name":"TransferSingle","type":"event"},
    {"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"operator","type":"address"},{"indexed":True,"internalType":"address","name":"from","type":"address"},{"indexed":True,"internalType":"address","name":"to","type":"address"},{"indexed":False,"internalType":"uint256[]","name":"ids","type":"uint256[]"},{"indexed":False,"internalType":"uint256[]","name":"values","type":"uint256[]"}],"name":"TransferBatch","type":"event"},
    {"inputs":[{"internalType":"address","name":"collateralToken","type":"address"},{"internalType":"bytes32","name":"parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}
]

@dataclass
class TradeSignal:
    timestamp: datetime
    token_id: str
    condition_id: str
    side: str
    size_shares: Decimal
    trader_address: str
    tx_hash: str
    question: str
    collateral_token: str

# --- Global Stats ---
class Stats:
    def __init__(self):
        self.start_time = datetime.now()
        self.events_received = 0
        self.events_processed = 0
        self.whitelist_matches = 0
        self.signals_generated = 0
        self.orders_attempted = 0
        self.orders_filled = 0
        self.orders_failed = 0
        self.orders_skipped_late_entry = 0
        self.orders_skipped_illiquid = 0
        self.orders_skipped_drawdown = 0
        self.last_event_time = None
        self.last_match_time = None
        self.unique_addresses_seen = set()
        self.unique_traders_matched = set()  # Track variety of traders we're copying
        self.wss_reconnects = 0
        
    def uptime(self) -> str:
        delta = datetime.now() - self.start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"

# Global stats instance
stats = Stats()

# --- Drawdown Protection ---
class DrawdownTracker:
    def __init__(self):
        self.daily_start_balance = Decimal("0")
        self.current_balance = Decimal("0")
        self.day_start = datetime.now().date()
        self.paused_until = None
        self.total_spent_today = Decimal("0")
        self.total_received_today = Decimal("0")
        
    def update_balance(self, new_balance: Decimal):
        today = datetime.now().date()
        
        # Reset daily tracking at midnight
        if today != self.day_start:
            self.day_start = today
            self.daily_start_balance = new_balance
            self.total_spent_today = Decimal("0")
            self.total_received_today = Decimal("0")
            logger.info(f"📅 New day - resetting daily P&L tracking. Start balance: ${new_balance:.2f}")
        
        # Set initial balance if not set
        if self.daily_start_balance == 0:
            self.daily_start_balance = new_balance
            
        self.current_balance = new_balance
    
    def record_trade(self, cost: Decimal, is_buy: bool):
        if is_buy:
            self.total_spent_today += cost
        else:
            self.total_received_today += cost
    
    def get_daily_pnl_pct(self) -> float:
        if self.daily_start_balance <= 0:
            return 0.0
        
        # Estimate P&L from balance change
        pnl = float(self.current_balance - self.daily_start_balance)
        pnl_pct = (pnl / float(self.daily_start_balance)) * 100
        return pnl_pct
    
    def check_and_pause(self) -> bool:
        """Returns True if trading should be paused"""
        # Check if already paused
        if self.paused_until and datetime.now() < self.paused_until:
            remaining = (self.paused_until - datetime.now()).total_seconds() / 60
            return True
        
        # Check for pause expiry
        if self.paused_until and datetime.now() >= self.paused_until:
            logger.info(f"✅ Drawdown pause expired - resuming trading")
            self.paused_until = None
            return False
        
        # Check drawdown
        pnl_pct = self.get_daily_pnl_pct()
        if pnl_pct < -DAILY_LOSS_LIMIT_PCT:
            self.paused_until = datetime.now() + timedelta(minutes=PAUSE_DURATION_MINUTES)
            logger.warning(f"🛑 DRAWDOWN LIMIT HIT: {pnl_pct:.1f}% daily loss. Pausing until {self.paused_until.strftime('%H:%M:%S')}")
            return True
        
        return False
    
    def get_status(self) -> str:
        pnl_pct = self.get_daily_pnl_pct()
        pnl_usd = float(self.current_balance - self.daily_start_balance)
        
        if self.paused_until:
            remaining = (self.paused_until - datetime.now()).total_seconds() / 60
            return f"⏸️ PAUSED ({remaining:.0f}m left) | Daily: {pnl_pct:+.1f}%"
        
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return f"{emoji} Daily P&L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})"

# Global drawdown tracker
drawdown = DrawdownTracker()

# --- Mirrored Positions Tracker ---
class MirroredPositions:
    """
    Tracks positions we opened by copying traders.
    Ensures we exit when they exit, even if position sync is delayed.
    """
    def __init__(self):
        # {token_id: {trader_address, entry_time, entry_price, shares, question}}
        self.positions: Dict[str, Dict] = {}
        self.lock = asyncio.Lock()
    
    async def record_entry(self, token_id: str, trader_addr: str, shares: int, price: float, question: str):
        """Record that we entered a position by copying a trader"""
        async with self.lock:
            if token_id in self.positions:
                # Add to existing position
                existing = self.positions[token_id]
                existing['shares'] += shares
                existing['traders'].add(trader_addr.lower())
                logger.debug(f"📝 Added to mirrored position: {token_id[:20]}... (+{shares} shares)")
            else:
                self.positions[token_id] = {
                    'traders': {trader_addr.lower()},
                    'entry_time': datetime.now(),
                    'entry_price': price,
                    'shares': shares,
                    'question': question[:50]
                }
                logger.info(f"📝 New mirrored position: {question[:30]}... ({shares} shares @ ${price:.2f})")
    
    async def should_exit(self, token_id: str, trader_addr: str) -> tuple[bool, int]:
        """
        Check if we should exit because a tracked trader is selling.
        Returns (should_exit, shares_to_sell)
        """
        async with self.lock:
            if token_id not in self.positions:
                return False, 0
            
            pos = self.positions[token_id]
            
            # If this trader was one of the ones we copied into this position, exit
            if trader_addr.lower() in pos['traders']:
                shares = pos['shares']
                logger.info(f"🚨 EXIT SIGNAL: {trader_addr[:10]}... selling position we copied!")
                return True, shares
            
            return False, 0
    
    async def record_exit(self, token_id: str, shares_sold: int):
        """Record that we exited (partially or fully)"""
        async with self.lock:
            if token_id in self.positions:
                self.positions[token_id]['shares'] -= shares_sold
                if self.positions[token_id]['shares'] <= 0:
                    del self.positions[token_id]
                    logger.info(f"📤 Closed mirrored position: {token_id[:20]}...")
    
    def get_all(self) -> Dict[str, Dict]:
        """Get all tracked positions (for display)"""
        return dict(self.positions)
    
    def get_position(self, token_id: str) -> Optional[Dict]:
        """Get a specific position by token_id"""
        return self.positions.get(token_id)
    
    def count(self) -> int:
        return len(self.positions)

# Global mirrored positions tracker
mirrored = MirroredPositions()

# --- Heartbeat System ---
class Heartbeat:
    def __init__(self, config: 'ConfigManager'):
        self.config = config
        self.running = False
        
    async def start(self):
        self.running = True
        logger.info(f"💓 Heartbeat started (interval: {HEARTBEAT_INTERVAL}s)")
        
        while self.running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            self.log_heartbeat()
    
    def log_heartbeat(self):
        now = datetime.now().strftime("%H:%M:%S")
        
        # Calculate events per minute
        uptime_seconds = (datetime.now() - stats.start_time).total_seconds()
        epm = (stats.events_received / uptime_seconds * 60) if uptime_seconds > 0 else 0
        
        # Time since last match
        last_match_ago = "never"
        if stats.last_match_time:
            secs = (datetime.now() - stats.last_match_time).total_seconds()
            last_match_ago = f"{int(secs)}s ago"
        
        # Skipped orders summary
        skipped_total = stats.orders_skipped_late_entry + stats.orders_skipped_illiquid + stats.orders_skipped_drawdown
        
        logger.info(
            f"💓 HEARTBEAT [{now}] | "
            f"Uptime: {stats.uptime()} | "
            f"Matches: {stats.whitelist_matches} | "
            f"Traders: {len(stats.unique_traders_matched)}/{len(self.config.addresses)} | "
            f"Orders: {stats.orders_filled}✓ {stats.orders_failed}✗ {skipped_total}⏭️ | "
            f"Last: {last_match_ago}"
        )
        
        # Show drawdown status
        logger.info(f"   {drawdown.get_status()} | Mirrored positions: {mirrored.count()}")
        
        # Show skip breakdown if any
        if skipped_total > 0:
            logger.info(f"   ⏭️ Skipped: {stats.orders_skipped_late_entry} late entry | {stats.orders_skipped_illiquid} illiquid | {stats.orders_skipped_drawdown} drawdown")
        
        # Show mirrored positions details if any
        if mirrored.count() > 0:
            positions = mirrored.get_all()
            pos_summaries = [f"{p['question'][:20]}..." for p in list(positions.values())[:3]]
            logger.info(f"   📋 Open positions: {', '.join(pos_summaries)}")
        
        # Show which traders we've matched (for variety check)
        if stats.unique_traders_matched:
            trader_names = [self.config.get_trader_name(addr) for addr in list(stats.unique_traders_matched)[-5:]]
            logger.info(f"   📊 Recent traders: {', '.join(trader_names)}")
        
        # In diagnostic mode, log unique addresses seen
        if DIAGNOSTIC_MODE and stats.unique_addresses_seen:
            sample = list(stats.unique_addresses_seen)[:5]
            logger.info(f"   🔍 DIAG: Seen {len(stats.unique_addresses_seen)} unique addresses. Sample: {sample}")
        
        # Reload config periodically
        self.config.load()

# --- Managers ---
class ConfigManager:
    def __init__(self, filepath="whitelist.json"):
        self.filepath = filepath
        self.traders = {}
        self.addresses = set()
        self.last_load = 0
        self.global_settings = {}
        self.load()

    def load(self):
        try:
            if not os.path.exists(self.filepath): 
                logger.warning(f"⚠️ Config file {self.filepath} not found.")
                return
            mtime = os.path.getmtime(self.filepath)
            if mtime <= self.last_load: return
            
            with open(self.filepath, 'r') as f:
                data = json.load(f)
            
            self.traders = {t['address'].lower(): t for t in data.get("whitelist", [])}
            self.addresses = set(self.traders.keys())
            self.global_settings = data.get("global_settings", {})
            self.last_load = mtime
            logger.info(f"🔄 Config Reloaded: Monitoring {len(self.addresses)} Traders")
            
            # Log global settings if present
            if self.global_settings:
                logger.info(f"   📋 Global settings from whitelist.json:")
                for k, v in self.global_settings.items():
                    logger.info(f"      {k}: {v}")
            
            # Log first few addresses for debugging
            if self.addresses:
                sample = list(self.addresses)[:3]
                logger.info(f"   Sample addresses: {sample}")
                
        except Exception as e:
            logger.error(f"❌ Config Load Error: {e}")

    def get(self, addr, key, default=None):
        # Sensible defaults for copy trading
        defaults = {
            "sizing_mode": "fixed",
            "fixed_amount": MAX_TRADE_USD,      # Target ~$1, actual $1-3
            "max_position_usdc": MAX_POSITION_USD,  # Allow 3-4 trades per market
            "mirror_fraction": 0.005,
            "max_slippage": 0.035,
        }
        default_val = defaults.get(key, default)
        
        # Get value from trader -> global_settings -> defaults
        trader_val = self.traders.get(addr.lower(), {}).get(key)
        global_val = self.global_settings.get(key)
        value = trader_val if trader_val is not None else (global_val if global_val is not None else default_val)
        
        # ENFORCE GLOBAL CAPS - override any whitelist values that are too high
        if key == "fixed_amount" and value > MAX_TRADE_USD:
            value = MAX_TRADE_USD
        if key == "max_position_usdc" and value > MAX_POSITION_USD:
            value = MAX_POSITION_USD
            
        return value
    
    def get_trader_name(self, addr):
        """Get trader's name from whitelist, or truncated address"""
        trader = self.traders.get(addr.lower(), {})
        name = trader.get("name", "")
        if name:
            # Extract just the username part (before ROI info)
            if " (ROI" in name:
                name = name.split(" (ROI")[0]
            return name[:20]
        return f"{addr[:10]}..."

class AssetManager:
    def __init__(self, w3: AsyncWeb3):
        self.w3 = w3
        self.market_cache = {}

    async def get_market_info(self, token_id: int) -> Optional[Dict[str, Any]]:
        """Fetch market info from multiple API endpoints with fallbacks."""
        tid_str = str(token_id)
        
        if tid_str in self.market_cache: 
            return self.market_cache[tid_str]

        loop = asyncio.get_running_loop()
        
        # Try Data API first
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{DATA_API}/markets",
                params={"clob_token_ids": tid_str},
                timeout=5
            ))
            if resp.ok:
                data = resp.json()
                if data and len(data) > 0:
                    m = data[0]
                    info = {
                        "question": m.get("question", m.get("title", "Unknown")),
                        "condition_id": m.get("condition_id"),
                        "collateral": m.get("collateral_token_address", "0x2791bca1f2de4661ed88a30c99a7a9449aa84174").lower(),
                        "active": m.get("active", True),
                        "closed": m.get("closed", False)
                    }
                    self.market_cache[tid_str] = info
                    return info
        except Exception as e:
            logger.debug(f"Data API lookup failed for {tid_str[:20]}...: {e}")

        # Try Gamma API as fallback
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{GAMMA_API}/markets",
                params={"clob_token_ids": tid_str},
                timeout=5
            ))
            if resp.ok:
                data = resp.json()
                if data and len(data) > 0:
                    m = data[0]
                    info = {
                        "question": m.get("question", m.get("title", "Unknown")),
                        "condition_id": m.get("conditionId", m.get("condition_id")),
                        "collateral": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC default
                        "active": m.get("active", True),
                        "closed": m.get("closed", False)
                    }
                    self.market_cache[tid_str] = info
                    return info
        except Exception as e:
            logger.debug(f"Gamma API lookup failed for {tid_str[:20]}...: {e}")

        # Try CLOB API
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{POLYMARKET_API}/markets/{tid_str}",
                timeout=5
            ))
            if resp.ok:
                m = resp.json()
                info = {
                    "question": m.get("question", "Unknown"),
                    "condition_id": m.get("condition_id"),
                    "collateral": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                    "active": m.get("active", True),
                    "closed": m.get("closed", False)
                }
                self.market_cache[tid_str] = info
                return info
        except Exception as e:
            logger.debug(f"CLOB API lookup failed for {tid_str[:20]}...: {e}")
            
        return None

class PositionsManager:
    """Fetch positions from Data API (not ClobClient)."""
    
    def __init__(self, funder_address: str):
        self.funder_address = funder_address
        
    async def get_positions(self) -> list:
        """Fetch user positions from Polymarket Data API."""
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{DATA_API}/positions",
                params={"user": self.funder_address},
                timeout=10
            ))
            if resp.ok:
                return resp.json()
            else:
                logger.warning(f"⚠️ Positions API returned {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"❌ Failed to fetch positions: {e}")
            return []

    async def get_collateral_balance(self, clob_client=None) -> Decimal:
        """Fetch USDC balance - try multiple methods."""
        loop = asyncio.get_running_loop()
        
        # Method 1: Try undocumented bets/value endpoint
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{DATA_API}/value",
                params={"user": self.funder_address},
                timeout=5
            ))
            if resp.ok:
                data = resp.json()
                # May have 'cash' or 'collateral' field
                bal = data.get("cash", data.get("collateral", data.get("balance", 0)))
                if bal and float(bal) > 0:
                    logger.info(f"💰 Balance from /value: ${bal}")
                    return Decimal(str(bal))
        except Exception as e:
            logger.debug(f"Value endpoint failed: {e}")
        
        # Method 2: Try profile endpoint
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{DATA_API}/profile/{self.funder_address}",
                timeout=5
            ))
            if resp.ok:
                data = resp.json()
                bal = data.get("balance", data.get("collateral", data.get("cash", 0)))
                if bal and float(bal) > 0:
                    logger.info(f"💰 Balance from /profile: ${bal}")
                    return Decimal(str(bal))
        except Exception as e:
            logger.debug(f"Profile endpoint failed: {e}")
            
        # Method 3: Gamma API profile
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{GAMMA_API}/users/{self.funder_address}",
                timeout=5
            ))
            if resp.ok:
                data = resp.json()
                bal = data.get("balance", data.get("collateral", data.get("cash", 0)))
                if bal and float(bal) > 0:
                    logger.info(f"💰 Balance from Gamma: ${bal}")
                    return Decimal(str(bal))
        except Exception as e:
            logger.debug(f"Gamma profile failed: {e}")
        
        # Method 4: Positions endpoint - sum up values
        try:
            positions = await self.get_positions()
            if positions:
                # Check if there's a cash/balance field in the response
                for p in positions:
                    if 'cashBalance' in p or 'balance' in p:
                        bal = p.get('cashBalance', p.get('balance', 0))
                        if bal and float(bal) > 0:
                            return Decimal(str(bal))
        except Exception as e:
            logger.debug(f"Position balance check failed: {e}")
            
        return Decimal(0)

# --- Secure Signer ---
class SecureSigner:
    def __init__(self, w3: AsyncWeb3):
        self.w3 = w3

    async def get_dynamic_gas(self) -> dict:
        try:
            block = await self.w3.eth.get_block("latest")
            base_fee = block['baseFeePerGas']
            priority_fee = await self.w3.eth.max_priority_fee
            max_fee = (base_fee * 2) + priority_fee
            return {'maxFeePerGas': max_fee, 'maxPriorityFeePerGas': priority_fee}
        except Exception:
            return {'maxFeePerGas': 500_000_000_000, 'maxPriorityFeePerGas': 40_000_000_000}

    async def sign_and_send(self, tx_params):
        pk = os.getenv("PRIVATE_KEY", "").strip()
        if not pk: raise ValueError("Missing Private Key")
        try:
            signed_tx = self.w3.eth.account.sign_transaction(tx_params, pk)
            tx_hash = await self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            return tx_hash
        finally:
            del pk 
            gc.collect()

# --- Auto Redeemer (Fixed) ---
class AutoRedeemer:
    def __init__(self, w3: AsyncWeb3, positions_mgr: PositionsManager, asset_mgr: AssetManager):
        self.w3 = w3
        self.positions_mgr = positions_mgr
        self.asset_mgr = asset_mgr
        self.signer = SecureSigner(w3)
        self.ctf_contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
        self.running = False

    async def redeem_cycle(self):
        try:
            logger.info("♻️  Scanning for redeemable winnings...")
            positions = await self.positions_mgr.get_positions()
            
            if not positions:
                logger.info("✅ No positions found.")
                return
            
            redeemable = []
            for p in positions:
                size = Decimal(str(p.get('size', 0)))
                if size <= 0: 
                    continue
                    
                token_id = p.get('asset', p.get('asset_id', p.get('tokenId')))
                if not token_id:
                    continue
                    
                info = await self.asset_mgr.get_market_info(int(token_id))
                if info and (info.get('closed') or not info.get('active')):
                    redeemable.append(info)

            seen = set()
            unique = []
            for r in redeemable:
                cid = r.get('condition_id')
                if cid and cid not in seen:
                    seen.add(cid)
                    unique.append(r)

            if not unique:
                logger.info("✅ No redemptions needed.")
                return

            for m in unique:
                await self.execute_redemption(m)

        except Exception as e:
            logger.error(f"❌ Redeemer Cycle Error: {e}")

    async def execute_redemption(self, market_info):
        try:
            logger.info(f"💰 REDEEMING: {market_info['question'][:40]}...")
            acct_addr = self.w3.to_checksum_address(FUNDER_ADDRESS)
            
            condition_id = market_info['condition_id']
            if condition_id.startswith('0x'):
                condition_id = condition_id[2:]
            
            func = self.ctf_contract.functions.redeemPositions(
                self.w3.to_checksum_address(market_info['collateral']),
                bytes(32), 
                bytes.fromhex(condition_id),
                [1, 2]
            )
            
            tx_params = await func.build_transaction({
                'from': acct_addr,
                'nonce': await self.w3.eth.get_transaction_count(acct_addr),
                'chainId': CHAIN_ID
            })
            
            gas_fees = await self.signer.get_dynamic_gas()
            tx_params.update(gas_fees)
            
            tx_hash = await self.signer.sign_and_send(tx_params)
            logger.info(f"✅ Redemption Success! Tx: {tx_hash.hex()}")
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"❌ Redemption Failed: {e}")

    async def start(self):
        self.running = True
        while self.running:
            await self.redeem_cycle()
            await asyncio.sleep(300)

# --- Risk Engine (Fixed) ---
class RiskEngine:
    def __init__(self, client: ClobClient, config: ConfigManager, positions_mgr: PositionsManager, w3: AsyncWeb3 = None):
        self.client = client
        self.config = config
        self.positions_mgr = positions_mgr
        self.w3 = w3
        self.positions = {}
        self.usdc_balance = Decimal(0)
        self.last_sync = 0

    async def sync_data(self):
        loop = asyncio.get_running_loop()
        try:
            logger.info("📥 Syncing Portfolio Data...")
            
            # Check for manual balance override first
            if MANUAL_BALANCE:
                try:
                    self.usdc_balance = Decimal(MANUAL_BALANCE)
                    logger.info(f"💰 Using MANUAL_BALANCE override: ${self.usdc_balance:.2f}")
                except:
                    pass
            else:
                # Try multiple methods to get balance
                self.usdc_balance = Decimal(0)
                
                # Method 1: PositionsManager (Data API)
                bal = await self.positions_mgr.get_collateral_balance(self.client)
                if bal > 0:
                    self.usdc_balance = bal
            
            # Method 2: On-chain USDC balance (try both USDC.e and native USDC)
            if self.usdc_balance == 0 and self.w3:
                usdc_addresses = [
                    ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC.e"),  # Bridged USDC (Polymarket uses this)
                    ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC"),    # Native USDC
                ]
                usdc_abi = [{"constant":True,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
                
                for usdc_address, usdc_name in usdc_addresses:
                    try:
                        usdc_contract = self.w3.eth.contract(
                            address=self.w3.to_checksum_address(usdc_address), 
                            abi=usdc_abi
                        )
                        raw_bal = await usdc_contract.functions.balanceOf(
                            self.w3.to_checksum_address(FUNDER_ADDRESS)
                        ).call()
                        bal = Decimal(raw_bal) / Decimal(10**6)  # USDC has 6 decimals
                        if bal > 0:
                            self.usdc_balance = bal
                            logger.info(f"💰 On-chain {usdc_name} balance: ${self.usdc_balance:.2f}")
                            break
                    except Exception as e:
                        logger.debug(f"On-chain {usdc_name} check failed: {e}")
            
            # Get positions from Data API
            pos_list = await self.positions_mgr.get_positions()
            self.positions = {}
            for p in pos_list:
                asset_id = str(p.get('asset', p.get('asset_id', p.get('tokenId', ''))))
                size = Decimal(str(p.get('size', 0)))
                if asset_id and size > 0:
                    self.positions[asset_id] = size
            
            self.last_sync = time.time()
            logger.info(f"💳 Balance: ${self.usdc_balance:.2f} | Positions: {len(self.positions)}")
            
            # Update drawdown tracker
            drawdown.update_balance(self.usdc_balance)
        except Exception as e:
            logger.error(f"❌ Risk Sync Error: {e}")

    async def get_execution_price(self, token_id: str, side: str) -> Optional[Decimal]:
        """Get best price for execution. Returns None if orderbook is empty."""
        loop = asyncio.get_running_loop()
        try:
            book = await loop.run_in_executor(None, lambda: self.client.get_order_book(token_id))

            if not book or not book.asks or not book.bids: 
                logger.warning(f"⚠️ SKIP {token_id[:20]}...: Empty Orderbook")
                return None

            best_ask = Decimal(str(book.asks[0].price))
            best_bid = Decimal(str(book.bids[0].price))
            return best_ask if side == "BUY" else best_bid
        except Exception as e:
            logger.error(f"❌ Orderbook fetch failed: {e}")
            return None

    async def check_orderbook_depth(self, token_id: str, side: str, size: int) -> tuple[bool, Decimal]:
        """
        Check if orderbook has enough depth for our trade.
        Returns (is_liquid, estimated_slippage)
        """
        loop = asyncio.get_running_loop()
        try:
            book = await loop.run_in_executor(None, lambda: self.client.get_order_book(token_id))
            
            if not book:
                return False, Decimal("1")
            
            # For BUY, check asks; for SELL, check bids
            orders = book.asks if side == "BUY" else book.bids
            
            if not orders:
                return False, Decimal("1")
            
            # Calculate total depth in USD at reasonable prices
            total_depth_usd = Decimal("0")
            best_price = Decimal(str(orders[0].price))
            
            # Consider orders within 5% of best price
            price_limit = best_price * Decimal("1.05") if side == "BUY" else best_price * Decimal("0.95")
            
            for order in orders[:10]:  # Check top 10 levels
                order_price = Decimal(str(order.price))
                order_size = Decimal(str(order.size))
                
                if side == "BUY" and order_price > price_limit:
                    break
                if side == "SELL" and order_price < price_limit:
                    break
                    
                total_depth_usd += order_price * order_size
            
            is_liquid = total_depth_usd >= Decimal(str(MIN_ORDERBOOK_DEPTH_USD))
            
            # Estimate slippage - if our size is > 20% of depth, high slippage
            our_cost = Decimal(size) * best_price
            slippage_risk = our_cost / total_depth_usd if total_depth_usd > 0 else Decimal("1")
            
            return is_liquid, slippage_risk
            
        except Exception as e:
            logger.debug(f"Depth check failed: {e}")
            return True, Decimal("0")  # Default to allowing trade

    def check_cumulative_limit(self, trader_addr, token_id, current_price, new_cost):
        max_pos = Decimal(str(self.config.get(trader_addr, "max_position_usdc")))
        
        current_shares = self.positions.get(token_id, Decimal(0))
        current_val = current_shares * current_price
        
        total_exposure = current_val + new_cost
        
        logger.info(f"📊 Risk Check: Cur(${current_val:.2f}) + New(${new_cost:.2f}) = ${total_exposure:.2f} (Limit: ${max_pos})")
        
        if total_exposure > max_pos:
            logger.warning(f"🛑 LIMIT HIT: ${total_exposure:.2f} > ${max_pos}. Skipping.")
            return False
        return True

# --- Execution Engine ---
class ExecutionEngine:
    def __init__(self, config: ConfigManager, risk: RiskEngine):
        self.config = config
        self.risk = risk
        self.queue = asyncio.Queue()
        self.running = False

    async def execute(self, signal: TradeSignal):
        try:
            trader_name = self.config.get_trader_name(signal.trader_address)
            logger.info(f"⚙️ Processing: {signal.side} for {signal.question[:30]}... (from {trader_name})")

            # === CHECK 1: DRAWDOWN PROTECTION (BUY only - always allow exits) ===
            if signal.side == "BUY" and drawdown.check_and_pause():
                stats.orders_skipped_drawdown += 1
                logger.warning(f"⏸️ SKIP: Trading paused due to drawdown limit")
                return

            # === CHECK 2: GET MARKET PRICE ===
            mkt_price = await self.risk.get_execution_price(signal.token_id, signal.side)
            if not mkt_price: 
                return

            # === CHECK 3: LATE ENTRY FILTER ===
            if signal.side == "BUY":
                if float(mkt_price) > MAX_ENTRY_PRICE:
                    stats.orders_skipped_late_entry += 1
                    logger.info(f"⏭️ SKIP: Late entry - price {mkt_price:.2f} > {MAX_ENTRY_PRICE} (little upside)")
                    return
                if float(mkt_price) < MIN_ENTRY_PRICE:
                    stats.orders_skipped_late_entry += 1
                    logger.info(f"⏭️ SKIP: Price too low - {mkt_price:.2f} < {MIN_ENTRY_PRICE} (too risky)")
                    return

            # === CHECK 4: SMART ORDER DELAY (BUY only - exits should be fast) ===
            if ORDER_DELAY_SECONDS > 0 and signal.side == "BUY":
                await asyncio.sleep(ORDER_DELAY_SECONDS)
                
                # Re-check price after delay to detect front-running
                new_price = await self.risk.get_execution_price(signal.token_id, signal.side)
                if new_price:
                    price_change = abs(float(new_price - mkt_price) / float(mkt_price))
                    if price_change > 0.02:  # >2% price move
                        logger.warning(f"⚠️ SKIP: Price moved {price_change:.1%} during delay (possible front-run)")
                        return
                    mkt_price = new_price

            addr = signal.trader_address
            
            # SIMPLIFIED: Always use fixed USD amount (capped by MAX_TRADE_USD)
            fixed_usd = Decimal(str(self.config.get(addr, "fixed_amount")))
            max_pos = Decimal(str(self.config.get(addr, "max_position_usdc")))
            
            # For SELL: smarter exit logic
            if signal.side == "SELL":
                # PRIORITY 1: Check mirrored positions tracker (positions we entered by copying)
                should_exit, mirrored_shares = await mirrored.should_exit(signal.token_id, signal.trader_address)
                
                if should_exit and mirrored_shares > 0:
                    # A trader we copied is exiting - sell our FULL mirrored position
                    logger.info(f"🚨 MIRRORED EXIT: Following {self.config.get_trader_name(addr)} out of position")
                    target_shares = Decimal(mirrored_shares)
                else:
                    # Check if we have this position from mirroring (even if should_exit returned False)
                    mirrored_pos = mirrored.get_position(signal.token_id)
                    our_position = self.risk.positions.get(signal.token_id, Decimal(0))
                    
                    if mirrored_pos:
                        # We have a mirrored position - sell all of it
                        target_shares = Decimal(mirrored_pos['shares'])
                        logger.info(f"📤 Exiting mirrored position: {target_shares} shares")
                    elif our_position > 0:
                        # Regular position (not from mirroring) - sell proportionally
                        target_shares = min(fixed_usd / mkt_price, our_position)
                    else:
                        logger.info(f"ℹ️ SKIP SELL: We don't hold this position")
                        return
            else:
                # BUY: fixed USD amount / market price = shares
                target_shares = fixed_usd / mkt_price

            target_shares = target_shares.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if target_shares < 1: 
                logger.warning(f"⚠️ SKIP: Target size {target_shares} too small.")
                return

            est_cost = (target_shares * mkt_price).quantize(Decimal("0.01"), rounding=ROUND_UP)
            
            # Polymarket minimum order is $1 - adjust shares up if needed
            MIN_ORDER_VALUE = Decimal("1.01")
            if est_cost < MIN_ORDER_VALUE:
                # Calculate minimum shares needed, round UP to 2 decimals
                target_shares = (MIN_ORDER_VALUE / mkt_price).quantize(Decimal("0.01"), rounding=ROUND_UP)
                est_cost = (target_shares * mkt_price).quantize(Decimal("0.01"), rounding=ROUND_UP)
                logger.info(f"📏 Adjusted to minimum: {target_shares} shares (${est_cost:.2f})")

            # === CHECK 5: ORDERBOOK DEPTH (BUY only - always allow exits) ===
            rounded_size = int(math.ceil(float(target_shares)))
            if signal.side == "BUY":
                is_liquid, slippage_risk = await self.risk.check_orderbook_depth(signal.token_id, signal.side, rounded_size)
                if not is_liquid:
                    stats.orders_skipped_illiquid += 1
                    logger.warning(f"💧 SKIP: Illiquid market (depth < ${MIN_ORDERBOOK_DEPTH_USD})")
                    return
                if slippage_risk > Decimal("0.3"):  # Our trade is >30% of available liquidity
                    logger.warning(f"⚠️ High slippage risk ({slippage_risk:.1%} of liquidity) - proceeding cautiously")
            
            if time.time() - self.risk.last_sync > 10:
                await self.risk.sync_data()

            if signal.side == "BUY":
                if est_cost > self.risk.usdc_balance:
                    logger.error(f"❌ SKIP: Insufficient Balance (${self.risk.usdc_balance:.2f} < ${est_cost:.2f})")
                    return

                if not self.risk.check_cumulative_limit(addr, signal.token_id, mkt_price, est_cost):
                    return

            slippage = Decimal(str(self.config.get(addr, "max_slippage", 0.035)))
            
            if signal.side == "BUY":
                limit_price = mkt_price * (1 + slippage)
                limit_price = min(limit_price, Decimal("0.99"))
            else:
                limit_price = mkt_price * (1 - slippage)
                limit_price = max(limit_price, Decimal("0.02"))

            # Round to proper precision
            # CRITICAL: price * size must have max 2 decimals
            # If price has 2 decimals, size must be whole number
            rounded_price = float(limit_price.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
            rounded_size = int(math.ceil(float(target_shares)))  # Always round UP to whole number
            
            # Ensure order value exceeds $1 minimum
            order_value = rounded_size * rounded_price
            if order_value < 1.0:
                rounded_size = int(math.ceil(1.01 / rounded_price))
                order_value = rounded_size * rounded_price
                logger.info(f"📏 Adjusted to minimum: {rounded_size} shares @ ${rounded_price} = ${order_value:.2f}")
            
            if rounded_size < 1:
                logger.warning(f"⚠️ SKIP: Size {rounded_size} too small")
                return
            
            logger.info(f"🚀 SENDING ORDER: {signal.side} {rounded_size} @ {rounded_price:.2f} (Mkt: {mkt_price:.4f}) = ${order_value:.2f}")
            
            stats.orders_attempted += 1
            
            order_args = OrderArgs(
                token_id=signal.token_id,
                price=rounded_price,
                size=rounded_size,
                side=BUY if signal.side == "BUY" else SELL
            )

            loop = asyncio.get_running_loop()
            
            # Need to get tick_size for the market first
            tick_size = "0.01"  # Default
            neg_risk = False
            
            try:
                tick_size = await loop.run_in_executor(
                    None,
                    lambda: self.risk.client.get_tick_size(signal.token_id)
                )
                logger.debug(f"Tick size for {signal.token_id[:20]}: {tick_size}")
            except Exception as e:
                logger.debug(f"Could not get tick_size: {e}")
            
            try:
                neg_risk = await loop.run_in_executor(
                    None,
                    lambda: self.risk.client.get_neg_risk(signal.token_id)
                )
            except:
                pass
            
            # Try to import the options class (different versions have different names)
            options = None
            try:
                from py_clob_client.clob_types import CreateOrderOptions
                options = CreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
            except (ImportError, TypeError):
                try:
                    from py_clob_client.clob_types import PartialCreateOrderOptions
                    options = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
                except (ImportError, TypeError):
                    # Fallback to dict-like object
                    class Options:
                        def __init__(self, tick_size, neg_risk):
                            self.tick_size = tick_size
                            self.neg_risk = neg_risk
                    options = Options(str(tick_size), neg_risk)
            
            # Create order with options, then post with order type
            try:
                # Create the signed order
                signed_order = await loop.run_in_executor(
                    None,
                    lambda: self.risk.client.create_order(order_args, options)
                )
                
                # Post with FAK order type
                resp = await loop.run_in_executor(
                    None,
                    lambda: self.risk.client.post_order(signed_order, OrderType.FAK)
                )
            except Exception as e:
                logger.warning(f"⚠️ Order with options failed: {e}")
                try:
                    # Fallback: try create_and_post without second arg
                    resp = await loop.run_in_executor(
                        None,
                        lambda: self.risk.client.create_and_post_order(order_args)
                    )
                except Exception as e2:
                    logger.error(f"❌ Order fallback also failed: {e2}")
                    stats.orders_failed += 1
                    return

            # Check response - handle both dict and object responses
            success = False
            if isinstance(resp, dict):
                success = resp.get('success', False)
                status = resp.get('status', '')
                error_msg = resp.get('errorMsg', resp.get('error_msg', ''))
                order_id = resp.get('orderID', resp.get('order_id', 'N/A'))
                tx_hashes = resp.get('transactionsHashes', [])
                taking = resp.get('takingAmount', '')
                making = resp.get('makingAmount', '')
            else:
                success = getattr(resp, 'success', False)
                status = getattr(resp, 'status', '')
                error_msg = getattr(resp, 'errorMsg', getattr(resp, 'error_msg', ''))
                order_id = getattr(resp, 'orderID', getattr(resp, 'order_id', 'N/A'))
                tx_hashes = getattr(resp, 'transactionsHashes', [])
                taking = getattr(resp, 'takingAmount', '')
                making = getattr(resp, 'makingAmount', '')
            
            if success or status == 'matched':
                stats.orders_filled += 1
                # Track the trade for drawdown P&L
                drawdown.record_trade(Decimal(str(order_value)), signal.side == "BUY")
                
                # Track mirrored positions for exit signals
                if signal.side == "BUY":
                    await mirrored.record_entry(
                        signal.token_id, 
                        signal.trader_address, 
                        rounded_size, 
                        rounded_price,
                        signal.question
                    )
                else:
                    await mirrored.record_exit(signal.token_id, rounded_size)
                
                if tx_hashes:
                    logger.info(f"✅ ORDER MATCHED! Tx: {tx_hashes[0][:20]}... | Taking: {taking} | Making: {making}")
                elif status == 'delayed':
                    logger.info(f"⏳ ORDER DELAYED (pending match) | OrderID: {order_id}")
                else:
                    logger.info(f"✅ ORDER SUCCESS | Status: {status} | OrderID: {order_id}")
            elif resp:
                stats.orders_failed += 1
                logger.error(f"❌ ORDER FAILED: {error_msg or status} (OrderID: {order_id})")
            else:
                stats.orders_failed += 1
                logger.error(f"❌ ORDER FAILED: No response received")

        except Exception as e:
            logger.error(f"❌ Execution Exception: {e}", exc_info=True)

    async def run(self):
        self.running = True
        while self.running:
            signal = await self.queue.get()
            await self.execute(signal)
            self.queue.task_done()

# --- WSS Monitor (Fixed with better debugging) ---
class WSSMonitor:
    def __init__(self, config: ConfigManager, engine: ExecutionEngine, asset_mgr: AssetManager):
        self.config = config
        self.engine = engine
        self.asset_mgr = asset_mgr
        self.running = False

    async def process_log(self, log, contract):
        try:
            stats.events_received += 1
            stats.last_event_time = datetime.now()
            
            event_data = None
            is_batch = False
            
            try:
                event_data = contract.events.TransferSingle().process_log(log)
            except:
                try:
                    event_data = contract.events.TransferBatch().process_log(log)
                    is_batch = True
                except:
                    return 

            stats.events_processed += 1
            args = event_data['args']
            src = args['from'].lower()
            dst = args['to'].lower()
            
            # Track unique addresses in diagnostic mode
            if DIAGNOSTIC_MODE:
                if src != "0x0000000000000000000000000000000000000000":
                    stats.unique_addresses_seen.add(src)
                if dst != "0x0000000000000000000000000000000000000000":
                    stats.unique_addresses_seen.add(dst)

            # Check for whitelist match
            trader, side = None, None
            if src in self.config.addresses:
                trader, side = src, "SELL"
            elif dst in self.config.addresses:
                trader, side = dst, "BUY"
            
            if not trader:
                return

            stats.whitelist_matches += 1
            stats.last_match_time = datetime.now()
            stats.unique_traders_matched.add(trader)
            trader_name = self.config.get_trader_name(trader)
            logger.info(f"🎯 MATCH: {trader_name} ({side})")

            ids = args['ids'] if is_batch else [args['id']]
            values = args['values'] if is_batch else [args['value']]

            for tid, raw_val in zip(ids, values):
                info = await self.asset_mgr.get_market_info(tid)
                if not info:
                    logger.warning(f"⚠️ SKIP: Market info failed for ID {str(tid)[:30]}...")
                    continue
                
                if not info.get('active', True):
                    logger.info(f"ℹ️ SKIP: Market inactive ({info['question'][:30]}...)")
                    continue
                
                # CTF tokens always use 6 decimals
                size_shares = Decimal(raw_val) / Decimal(10**CTF_DECIMALS)
                
                if size_shares < Decimal("0.5"): 
                    logger.info(f"ℹ️ SKIP: Dust trade ({size_shares:.2f} shares)")
                    continue

                signal = TradeSignal(
                    timestamp=datetime.now(),
                    token_id=str(tid),
                    condition_id=info.get('condition_id', ''),
                    side=side,
                    size_shares=size_shares,
                    trader_address=trader,
                    tx_hash=log['transactionHash'].hex(),
                    question=info['question'],
                    collateral_token=info['collateral']
                )
                
                trader_name = self.config.get_trader_name(trader)
                stats.signals_generated += 1
                logger.info(f"🔔 SIGNAL: {trader_name} | {side} {size_shares:.1f} shares | {info['question'][:40]}...")
                await self.engine.queue.put(signal)

        except Exception as e:
            logger.error(f"❌ Log Processing Error: {e}", exc_info=True)

    async def start(self):
        self.running = True
        while self.running:
            try:
                logger.info(f"🔌 Connecting WSS: {POLYGON_WSS[:50]}...")
                stats.wss_reconnects += 1
                
                async with AsyncWeb3(WebSocketProvider(POLYGON_WSS)) as w3:
                    if not await w3.is_connected():
                        raise ConnectionError("WSS Handshake Failed")
                    
                    contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
                    sub_id = await w3.eth.subscribe("logs", {"address": CTF_ADDRESS})
                    logger.info(f"✅ WSS Connected & Subscribed (sub_id: {sub_id})")
                    
                    async for payload in w3.socket.process_subscriptions():
                        # Handle different payload formats
                        result = None
                        if isinstance(payload, dict):
                            result = payload.get("result")
                            if not result and "params" in payload:
                                result = payload["params"].get("result")
                        
                        if result:
                            await self.process_log(result, contract)
            
            except asyncio.CancelledError:
                logger.info("🛑 WSS Monitor cancelled")
                break
            except Exception as e:
                logger.error(f"⚠️ WSS Disconnected: {e}. Retry in 5s...")
                await asyncio.sleep(5)

# --- Main ---
async def main():
    logger.info("=" * 50)
    logger.info("=== 🦅 POLYMARKET MIRROR BOT V10 ===")
    logger.info("=" * 50)
    
    if DIAGNOSTIC_MODE:
        logger.info("🔍 DIAGNOSTIC MODE ENABLED - Will log all unique addresses seen")
    
    config = ConfigManager()
    
    if not config.addresses:
        logger.warning("⚠️ No traders in whitelist! Add addresses to whitelist.json")
    
    # Init Web3 with POA middleware for Polygon
    w3_http = AsyncWeb3(AsyncHTTPProvider(POLYGON_HTTP))
    
    # Inject POA middleware for Polygon
    try:
        w3_http.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        logger.info("✅ POA middleware injected")
    except Exception as e:
        logger.warning(f"⚠️ Could not inject POA middleware: {e}")
    
    if not await w3_http.is_connected():
        logger.critical("❌ HTTP RPC Connection Failed")
        return
    logger.info("✅ HTTP RPC Connected")
        
    asset_mgr = AssetManager(w3_http)
    positions_mgr = PositionsManager(FUNDER_ADDRESS)
    
    # Init CLOB Client
    pk = os.getenv("PRIVATE_KEY", "").strip()
    if not pk:
        logger.critical("❌ Missing PRIVATE_KEY")
        return
        
    clob_client = ClobClient(
        POLYMARKET_API, 
        key=pk, 
        chain_id=CHAIN_ID, 
        signature_type=1, 
        funder=FUNDER_ADDRESS
    )
    
    try:
        clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
        logger.info("✅ CLOB API credentials set")
    except Exception as e:
        logger.warning(f"⚠️ API creds issue (may still work): {e}")
    
    del pk
    gc.collect()

    # Init Components - pass w3 to RiskEngine for on-chain balance check
    risk = RiskEngine(clob_client, config, positions_mgr, w3_http)
    engine = ExecutionEngine(config, risk)
    monitor = WSSMonitor(config, engine, asset_mgr)
    heartbeat = Heartbeat(config)
    
    # Auto-redeemer disabled by default (set ENABLE_REDEEMER=true to enable)
    enable_redeemer = os.getenv("ENABLE_REDEEMER", "false").lower() == "true"
    
    # Initial sync
    await risk.sync_data()
    
    logger.info("🚀 Starting monitoring loops...")
    logger.info(f"   📋 Watching {len(config.addresses)} addresses")
    logger.info(f"   💓 Heartbeat every {HEARTBEAT_INTERVAL}s")
    logger.info(f"   ♻️  Auto-redeemer: {'ENABLED' if enable_redeemer else 'DISABLED'}")
    logger.info(f"   💰 Target trade: ~${MAX_TRADE_USD:.2f} (actual $1-3 due to whole shares)")
    logger.info(f"   📊 Max position: ${MAX_POSITION_USD} per market")
    logger.info(f"   🎯 Entry filter: {MIN_ENTRY_PRICE*100:.0f}%-{MAX_ENTRY_PRICE*100:.0f}% only")
    logger.info(f"   ⏱️ Order delay: {ORDER_DELAY_SECONDS}s (front-run protection)")
    logger.info(f"   💧 Min liquidity: ${MIN_ORDERBOOK_DEPTH_USD}")
    logger.info(f"   🛑 Drawdown limit: {DAILY_LOSS_LIMIT_PCT}% daily (pause {PAUSE_DURATION_MINUTES}m)")
    logger.info(f"   🔄 Auto-exit: ENABLED (will sell when copied traders exit)")
    
    tasks = [
        monitor.start(),
        engine.run(),
        heartbeat.start()
    ]
    
    if enable_redeemer:
        redeemer = AutoRedeemer(w3_http, positions_mgr, asset_mgr)
        tasks.append(redeemer.start())
    
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested.")