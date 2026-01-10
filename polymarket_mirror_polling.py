#!/usr/bin/env python3
"""
Polymarket Mirror Bot (Ludicrous Speed Edition)
-----------------------------------------------
1. ZERO LATENCY: No polling sleep. Loops instantly upon response.
2. FIRE-FIRST: Executes trade BEFORE logging to console (saves ~5ms).
3. INFRASTRUCTURE: Recommended to run on AWS US-East-1 VPS.
"""

import os
import time
import math
import logging
import threading
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- CONFIGURATION ---
BUY = "BUY"
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI = '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"}]'

# Libraries Check
try:
    from web3 import Web3
    from eth_account import Account
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False

load_dotenv()

# --- SETTINGS ---
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "") 
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

# --- MULTIPLIER ---
POSITION_MULTIPLIER = Decimal(os.getenv("POSITION_MULTIPLIER", "2.0"))

# Risk Management
MAX_CAPITAL_USAGE = Decimal(os.getenv("MAX_CAPITAL_TO_USE", "5000"))
MAX_BUY_PRICE = Decimal("0.99")

# --- SMART TOLERANCE ---
MAX_UPPER_SLIPPAGE = Decimal("0.05") 
MAX_LOWER_SLIPPAGE = Decimal("0.15") 

# Execution Limits
MIN_TRADE_USD = Decimal("1.05") 
MIN_SHARES_COUNT = Decimal("5.0")

# SPEED SETTINGS
POLL_INTERVAL = 0.0          # ZERO SLEEP (Ludicrous Mode)
BALANCE_REFRESH_SEC = 60     
POSITION_THRESHOLD = Decimal("0.0001")

# Endpoints
POLYMARKET_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s', # Short log format saves console time
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Mute libraries
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    size: Decimal
    avg_price: Decimal
    cur_price: Decimal  
    market_question: str
    end_date: str

class PolymarketMirror:
    def __init__(self, target_address: str, private_key: str):
        self.target_address = target_address.lower() if target_address else ""
        self.private_key = private_key
        self.target_positions: dict[str, Position] = {} 
        self.initialized = False
        self.last_heartbeat = 0
        self.total_spent = Decimal(0)
        self.blacklisted_tokens = set()

        self.cached_target_equity = Decimal(0)
        self.cached_my_cash = Decimal(0)
        self.last_balance_update = 0
        
        # SPEED: Aggressive Connection Pooling
        self.session = requests.Session()
        # High pool size, low retries for fail-fast
        adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=Retry(total=1, backoff_factor=0.1))
        self.session.mount('https://', adapter)
        
        if WEB3_AVAILABLE:
            self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
            self.usdc_contract = self.w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
        else:
            self.w3 = None

        if CLOB_AVAILABLE and private_key:
            try:
                funder = FUNDER_ADDRESS if FUNDER_ADDRESS else Account.from_key(private_key).address
                self.my_address = funder.lower()
                self.client = ClobClient(POLYMARKET_API, key=private_key, chain_id=CHAIN_ID, signature_type=SIGNATURE_TYPE, funder=funder)
                try: self.client.set_api_creds(self.client.derive_api_key())
                except: pass 
                logger.info(f"✅ Ready: {self.my_address}")
            except:
                self.client = None
                self.my_address = ""
        else:
            self.client = None
            self.my_address = ""

    def get_usdc_balance_web3(self, address: str) -> Decimal:
        if not self.w3: return Decimal(0)
        try:
            raw = self.usdc_contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
            return Decimal(str(raw)) / Decimal("1000000")
        except: return Decimal(0)

    def get_all_positions(self, address: str) -> dict[str, Position]:
        pos_map = {}
        today_str = datetime.now().strftime("%Y-%m-%d")
        limit = 500 
        offset = 0
        
        while True:
            try:
                params = {"user": address, "limit": limit, "offset": offset} 
                # Reduced timeout to fail fast if network is choking
                resp = self.session.get(f"{DATA_API}/positions", params=params, timeout=2.0)
                
                if not resp.ok: 
                    if resp.status_code == 404: break 
                    return None # Abort on error to prevent hallucination
                
                data = resp.json()
                if not isinstance(data, list): break
                if len(data) == 0: break
                
                for p in data:
                    size = Decimal(str(p.get("size", 0)))
                    if size < 0.001: continue
                    
                    end_date = p.get("endDate")
                    if end_date and end_date < today_str: continue

                    tid = p.get("asset") or p.get("asset_id") or p.get("tokenId")
                    if not tid: continue
                    
                    if tid in self.blacklisted_tokens: continue

                    pos_map[tid] = Position(
                        market_id=p.get("marketId", ""),
                        token_id=tid,
                        outcome=p.get("outcome", ""),
                        size=size,
                        avg_price=Decimal(str(p.get("avgPrice", 0))),
                        cur_price=Decimal(str(p.get("curPrice", 0))),
                        market_question=p.get("title") or p.get("question", "Unknown"),
                        end_date=end_date or ""
                    )
                
                if len(data) < limit: break
                offset += limit
                
            except Exception:
                return None
        return pos_map

    def refresh_balances(self):
        try:
            target_cash = self.get_usdc_balance_web3(self.target_address)
            pos_val = sum(p.size * (p.cur_price if p.cur_price > 0 else p.avg_price) for p in self.target_positions.values())
            self.cached_target_equity = target_cash + pos_val
            
            if self.client:
                try: 
                    self.cached_my_cash = Decimal(self.client.get_balance_allowance(asset_type="COLLATERAL")['balance']) / Decimal("1000000")
                except: 
                    self.cached_my_cash = self.get_usdc_balance_web3(self.my_address)
            
            self.last_balance_update = time.time()
            logger.info(f"💰 Equity Updated. Target: ${self.cached_target_equity:,.0f} | Me: ${self.cached_my_cash:,.0f}")
        except: pass

    def place_trade(self, token_id: str, size: Decimal, side: str, limit_price: Decimal) -> bool:
        if not self.client: return False
        if self.total_spent >= MAX_CAPITAL_USAGE and side == BUY: return False

        try:
            raw_price = float(limit_price)
            if raw_price <= 0.001: return False 

            if side == BUY and raw_price > float(MAX_BUY_PRICE): return False

            # --- PRECISION & FLOOR ---
            price_str = f"{raw_price:.2f}"
            final_price = float(price_str)
            if final_price <= 0: return False

            # Calc Size
            raw_size = float(size)
            cost = raw_size * final_price
            
            # Force $1.05 floor
            if cost < 1.05:
                raw_size = 1.05 / final_price
                
            # Round to 1 decimal place (Safe)
            final_size = math.ceil(raw_size * 10) / 10.0
            if final_size < 5.0: final_size = 5.0
            
            # Double check cost floor after rounding
            if (final_size * final_price) < 1.0:
                 final_size = 1.05 / final_price
                 final_size = math.ceil(final_size * 10) / 10.0

            # --- EXECUTE FIRST, LOG LATER ---
            order_args = OrderArgs(price=final_price, size=final_size, side=side, token_id=token_id)
            signed_order = self.client.create_order(order_args)
            
            # Fire!
            resp = self.client.post_order(signed_order, orderType=OrderType.FOK)
            
            # Now we can take time to log
            if resp and resp.get("success"):
                logger.info(f"🚀 FILLED: {final_size} @ {final_price} (Tx: {resp.get('transactionHash')})")
                if side == BUY:
                    self.total_spent += Decimal(final_size) * Decimal(final_price)
                    self.cached_my_cash -= (Decimal(final_size) * Decimal(final_price))
                return True
            else:
                 error_msg = str(resp)
                 if "does not exist" in error_msg:
                     self.blacklisted_tokens.add(token_id)
                 logger.error(f"❌ Missed: {error_msg}")
                 return False

        except Exception as e:
            logger.error(f"Trade Error: {e}")
            return False

    def sync_positions(self):
        if time.time() - self.last_balance_update > BALANCE_REFRESH_SEC:
            self.refresh_balances()

        current_positions = self.get_all_positions(self.target_address)
        if current_positions is None: return 

        if not self.initialized:
            self.target_positions = current_positions
            self.refresh_balances() 
            self.initialized = True
            logger.info(f"📍 Ready. Tracking {len(current_positions)} positions.")
            return

        target_equity = self.cached_target_equity

        # --- BUYS ONLY ---
        for tid, curr_pos in current_positions.items():
            prev_pos = self.target_positions.get(tid)
            prev_size = prev_pos.size if prev_pos else Decimal(0)
            
            if curr_pos.size > prev_size + POSITION_THRESHOLD:
                delta = curr_pos.size - prev_size
                
                target_entry = curr_pos.avg_price
                if target_entry <= 0: target_entry = curr_pos.cur_price
                curr_price = curr_pos.cur_price
                
                if curr_price <= 0: continue

                # Optimization: Don't log "Detected" yet. Check criteria first.
                
                if target_entry > 0:
                    # Smart Tolerance Checks
                    if curr_price > target_entry + MAX_UPPER_SLIPPAGE: continue
                    if curr_price < target_entry - MAX_LOWER_SLIPPAGE: continue

                    my_cash = self.cached_my_cash
                    ratio = (my_cash / target_equity) if target_equity > 0 else 0
                    
                    target_spend_usd = delta * target_entry 
                    my_usd_size = (target_spend_usd * ratio) * POSITION_MULTIPLIER
                    
                    limit_price = target_entry + MAX_UPPER_SLIPPAGE
                    if limit_price > 0:
                        shares_to_buy = my_usd_size / limit_price
                        
                        # FIRE TRADE IMMEDIATELY
                        success = self.place_trade(tid, shares_to_buy, BUY, limit_price=limit_price)
                        
                        # Log Result
                        if success:
                            logger.info(f"🔔 COPIED BUY: {curr_pos.market_question[:30]}...")
                        else:
                            logger.info(f"⚠️ FAILED BUY: {curr_pos.market_question[:30]}...")

                self.target_positions[tid] = curr_pos

        # --- UPDATE TRACKER ---
        for tid in list(self.target_positions.keys()):
            if tid not in current_positions:
                del self.target_positions[tid]
            else:
                self.target_positions[tid] = current_positions[tid]

        for tid, curr_pos in current_positions.items():
            if tid not in self.target_positions:
                self.target_positions[tid] = curr_pos

        if time.time() - self.last_heartbeat > 30:
            logger.info(f"💓 Scan Active...")
            self.last_heartbeat = time.time()

    def run(self):
        logger.info(f"⚡ Ludicrous Mode Started (0ms Latency)")
        while True:
            try: self.sync_positions()
            except Exception: 
                time.sleep(0.1) # Brief sleep on crash
            # NO SLEEP HERE for Ludicrous Mode

if __name__ == "__main__":
    if not PRIVATE_KEY or not TARGET_ADDRESS:
        print("❌ Error: Set PRIVATE_KEY/TARGET_ADDRESS in .env")
    else:
        bot = PolymarketMirror(TARGET_ADDRESS, PRIVATE_KEY)
        bot.run()