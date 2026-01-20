#!/usr/bin/env python3
"""
Polymarket Mirror Bot (V8.3 - Heartbeat + Diagnostics)
------------------------------------------------------
✅ FIX: Use Data API for positions (not ClobClient.get_positions)
✅ FIX: Improved market info lookup with fallback
✅ FIX: Better debug logging for trade matching
✅ FIX: Collateral decimals (CTF tokens use 6 decimals, not collateral decimals)
✅ NEW: Heartbeat system with stats
✅ NEW: Diagnostic mode to log all addresses seen
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
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import requests

# --- Dependency Checks ---
try:
    from web3 import AsyncWeb3, AsyncHTTPProvider, WebSocketProvider
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
except ImportError as e:
    print(f"CRITICAL: Failed to import Polymarket CLOB client - {e}")
    print("FIX: pip install py-clob-client")
    exit(1)

load_dotenv()

# --- Logging Setup ---
log_file = "bot_v8.3.log"
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
logger = logging.getLogger("PolyV8.3")

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

POLYGON_WSS = get_env_strict("POLYGON_WS")
if not POLYGON_WSS.startswith("wss://"):
    logger.critical(f"❌ CONFIG ERROR: POLYGON_WS must start with 'wss://'. Got: {POLYGON_WSS[:10]}...")
    raise ValueError("Invalid WSS URL Scheme")

POLYGON_HTTP = get_env_strict("POLYGON_HTTP", "https://polygon-rpc.com")
CHAIN_ID = 137

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
        self.last_event_time = None
        self.last_match_time = None
        self.unique_addresses_seen = set()
        self.wss_reconnects = 0
        
    def uptime(self) -> str:
        delta = datetime.now() - self.start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"

# Global stats instance
stats = Stats()

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
        
        # Time since last event/match
        last_event_ago = "never"
        if stats.last_event_time:
            secs = (datetime.now() - stats.last_event_time).total_seconds()
            last_event_ago = f"{int(secs)}s ago"
            
        last_match_ago = "never"
        if stats.last_match_time:
            secs = (datetime.now() - stats.last_match_time).total_seconds()
            last_match_ago = f"{int(secs)}s ago"
        
        logger.info(
            f"💓 HEARTBEAT [{now}] | "
            f"Uptime: {stats.uptime()} | "
            f"Events: {stats.events_received} ({epm:.1f}/min) | "
            f"Matches: {stats.whitelist_matches} | "
            f"Orders: {stats.orders_filled}✓ {stats.orders_failed}✗ | "
            f"Last event: {last_event_ago} | "
            f"Last match: {last_match_ago}"
        )
        
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
            
            # Log first few addresses for debugging
            if self.addresses:
                sample = list(self.addresses)[:3]
                logger.info(f"   Sample addresses: {sample}")
                
        except Exception as e:
            logger.error(f"❌ Config Load Error: {e}")

    def get(self, addr, key, default=None):
        return self.traders.get(addr.lower(), {}).get(key, self.global_settings.get(key, default))

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

    async def get_collateral_balance(self) -> Decimal:
        """Fetch USDC balance from Data API."""
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{DATA_API}/balance",
                params={"user": self.funder_address},
                timeout=5
            ))
            if resp.ok:
                data = resp.json()
                return Decimal(str(data.get("balance", 0)))
        except Exception as e:
            logger.debug(f"Balance lookup via Data API failed: {e}")
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
    def __init__(self, client: ClobClient, config: ConfigManager, positions_mgr: PositionsManager):
        self.client = client
        self.config = config
        self.positions_mgr = positions_mgr
        self.positions = {}
        self.usdc_balance = Decimal(0)
        self.last_sync = 0

    async def sync_data(self):
        loop = asyncio.get_running_loop()
        try:
            logger.info("📥 Syncing Portfolio Data...")
            
            # Get balance from CLOB client
            try:
                bal = await loop.run_in_executor(None, self.client.get_collateral_balance)
                self.usdc_balance = Decimal(str(bal)) if bal else Decimal(0)
            except Exception as e:
                logger.warning(f"⚠️ CLOB balance fetch failed: {e}")
                self.usdc_balance = await self.positions_mgr.get_collateral_balance()
            
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
        except Exception as e:
            logger.error(f"❌ Risk Sync Error: {e}")

    async def get_execution_price(self, token_id: str, side: str) -> Optional[Decimal]:
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

    def check_cumulative_limit(self, trader_addr, token_id, current_price, new_cost):
        max_pos = Decimal(str(self.config.get(trader_addr, "max_position_usdc", 50)))
        
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
            logger.info(f"⚙️ Processing Signal: {signal.side} for {signal.question[:30]}...")

            mkt_price = await self.risk.get_execution_price(signal.token_id, signal.side)
            if not mkt_price: 
                return

            addr = signal.trader_address
            mode = self.config.get(addr, "sizing_mode", "fixed")
            
            target_shares = Decimal(0)
            if mode == "proportional":
                fraction = Decimal(str(self.config.get(addr, "mirror_fraction", 0.1)))
                target_shares = signal.size_shares * fraction
            else:
                fixed_usd = Decimal(str(self.config.get(addr, "fixed_amount", 10)))
                target_shares = fixed_usd / mkt_price

            target_shares = target_shares.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
            if target_shares < 1: 
                logger.warning(f"⚠️ SKIP: Target size {target_shares} too small.")
                return

            est_cost = target_shares * mkt_price
            
            if time.time() - self.risk.last_sync > 10:
                await self.risk.sync_data()

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

            logger.info(f"🚀 SENDING ORDER: {signal.side} {target_shares} @ {limit_price:.3f} (Mkt: {mkt_price:.3f})")
            
            stats.orders_attempted += 1
            
            order_args = OrderArgs(
                token_id=signal.token_id,
                price=float(limit_price),
                size=float(target_shares),
                side=BUY if signal.side == "BUY" else SELL,
                order_type=OrderType.FAK 
            )

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: self.risk.client.create_and_post_order(order_args))

            if resp and getattr(resp, 'success', False):
                stats.orders_filled += 1
                filled = Decimal(str(resp.size_matched)) if hasattr(resp, 'size_matched') else target_shares
                if filled < target_shares:
                    logger.warning(f"⚠️ PARTIAL FILL: {filled}/{target_shares} shares")
                else:
                    logger.info(f"✅ FULL FILL: {filled} shares executed.")
            else:
                stats.orders_failed += 1
                err = getattr(resp, 'errorMsg', str(resp))
                logger.error(f"❌ ORDER FAILED: {err}")

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
            logger.info(f"🎯 MATCH FOUND: {trader[:10]}... ({side})")

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
                
                stats.signals_generated += 1
                logger.info(f"🔔 SIGNAL: {trader[:8]}... {side} {size_shares:.1f} shares | {info['question'][:40]}...")
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
    logger.info("=== 🦅 POLYMARKET MIRROR BOT V8.3 ===")
    logger.info("=" * 50)
    
    if DIAGNOSTIC_MODE:
        logger.info("🔍 DIAGNOSTIC MODE ENABLED - Will log all unique addresses seen")
    
    config = ConfigManager()
    
    if not config.addresses:
        logger.warning("⚠️ No traders in whitelist! Add addresses to whitelist.json")
    
    # Init Web3
    w3_http = AsyncWeb3(AsyncHTTPProvider(POLYGON_HTTP))
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

    # Init Components
    risk = RiskEngine(clob_client, config, positions_mgr)
    engine = ExecutionEngine(config, risk)
    monitor = WSSMonitor(config, engine, asset_mgr)
    redeemer = AutoRedeemer(w3_http, positions_mgr, asset_mgr)
    heartbeat = Heartbeat(config)
    
    # Initial sync
    await risk.sync_data()
    
    logger.info("🚀 Starting monitoring loops...")
    logger.info(f"   📋 Watching {len(config.addresses)} addresses")
    logger.info(f"   💓 Heartbeat every {HEARTBEAT_INTERVAL}s")
    
    await asyncio.gather(
        monitor.start(),
        engine.run(),
        redeemer.start(),
        heartbeat.start()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested.")