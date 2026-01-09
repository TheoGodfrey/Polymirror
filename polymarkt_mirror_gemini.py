#!/usr/bin/env python3
"""
Polymarket Mirror Bot (Ultimate Edition - Stable)
----------------------------------------
1. EXECUTION: Fill-Or-Kill (FOK). Orders fill immediately or are cancelled.
2. POSITIVE SLIPPAGE: Buys cheaper than target if possible.
3. CRASH GUARD: Skips trades if price drops >10% (avoids falling knives).
4. SMART TOLERANCE: Only buys if price is within safe range (max +2% upside).
5. RELIABLE: Enforces API minimums ($1.00 / 5 shares) and fixes iteration crashes.
"""

import os
import time
import json
import logging
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime
from dotenv import load_dotenv
import requests

# --- CONFIGURATION ---
BUY = "BUY"
SELL = "SELL"
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
    print("⚠️ Web3 not found. Run: pip install web3")

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    print("⚠️ py_clob_client not found. Run: pip install py-clob-client")

load_dotenv()

# --- SETTINGS ---
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "") # Optional proxy
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1")) # 1=EOA, 2=Gnosis

# Risk Management
MAX_CAPITAL_USAGE = Decimal(os.getenv("MAX_CAPITAL_TO_USE", "5000"))
MAX_BUY_PRICE = Decimal("0.95")     # Don't buy arb/wager > 95 cents

# --- SMART TOLERANCE SETTINGS ---
# Upside: Max 2 cents over target's price (Avoid chasing pumps)
MAX_UPPER_SLIPPAGE = Decimal("0.02") 
# Downside: Max 10 cents under target's price (Crash Guard)
MAX_LOWER_SLIPPAGE = Decimal("0.10") 
# --------------------------------

# API Execution Limits
MIN_TRADE_USD = Decimal("1.00")     # API enforces ~$1 min value
MIN_SHARES_COUNT = Decimal("5.0")   # API often enforces 5 share min

POLL_INTERVAL = 0.5
POSITION_THRESHOLD = Decimal("0.0001")

# Endpoints
POLYMARKET_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

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
        self.my_positions: dict[str, Position] = {} 
        self.initialized = False
        self.last_heartbeat = 0
        self.total_spent = Decimal(0)
        
        # Web3 Setup (for reading USDC balance directly)
        if WEB3_AVAILABLE:
            self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
            self.usdc_contract = self.w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
        else:
            self.w3 = None

        # CLOB Client Setup (for trading)
        if CLOB_AVAILABLE and private_key:
            try:
                funder = FUNDER_ADDRESS if FUNDER_ADDRESS else Account.from_key(private_key).address
                self.my_address = funder.lower()
                self.client = ClobClient(POLYMARKET_API, key=private_key, chain_id=CHAIN_ID, signature_type=SIGNATURE_TYPE, funder=funder)
                try: self.client.set_api_creds(self.client.derive_api_key())
                except: pass 
                logger.info(f"✅ Client Initialized for: {self.my_address}")
            except Exception as e:
                logger.error(f"Client init error: {e}")
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
        """Fetches active positions from Data API"""
        pos_map = {}
        today_str = datetime.now().strftime("%Y-%m-%d")
        limit = 500 
        offset = 0
        
        while True:
            try:
                params = {"user": address, "limit": limit, "offset": offset} 
                resp = requests.get(f"{DATA_API}/positions", params=params, timeout=5)
                if not resp.ok: 
                    # If 404/Empty, just break (new wallets might return empty)
                    if resp.status_code == 404: break
                    raise Exception(f"API Error {resp.status_code}")
                
                data = resp.json()
                if not isinstance(data, list): break
                if len(data) == 0: break
                
                for p in data:
                    size = Decimal(str(p.get("size", 0)))
                    if size < 0.001: continue
                    
                    # Filter expired markets
                    end_date = p.get("endDate")
                    if end_date and end_date < today_str: continue

                    tid = p.get("asset") or p.get("asset_id") or p.get("tokenId")
                    if not tid: continue
                    
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
                
            except Exception as e:
                logger.error(f"Fetch Error: {e}")
                # Don't crash loop on read error
                break
        return pos_map

    def get_portfolio_equity(self, address: str, positions: dict = None) -> Decimal:
        cash = self.get_usdc_balance_web3(address)
        if positions is None:
            try: positions = self.get_all_positions(address)
            except: positions = {}
        pos_val = sum(p.size * (p.cur_price if p.cur_price > 0 else p.avg_price) for p in positions.values())
        return cash + pos_val

    def get_my_cash(self) -> Decimal:
        if self.client:
            try: return Decimal(self.client.get_balance_allowance(asset_type="COLLATERAL")['balance']) / Decimal("1000000")
            except: pass
        return self.get_usdc_balance_web3(Account.from_key(self.private_key).address)

    def place_trade(self, token_id: str, size: Decimal, side: str, limit_price: Decimal) -> bool:
        """Executes a Fill-Or-Kill (FOK) order with force minimums."""
        if not self.client: return False
        if self.total_spent >= MAX_CAPITAL_USAGE and side == BUY:
            logger.warning(f"🛑 Max capital limit reached. Skipping.")
            return False

        try:
            price = float(limit_price)
            if price <= 0: return False

            if side == BUY and price > float(MAX_BUY_PRICE):
                logger.info(f"🚫 Price {price:.3f} > Safety Cap. Skipping.")
                return False

            # --- FORCE MINIMUMS (API Compliance) ---
            # 1. Enforce Share Count Minimum
            safe_size = max(float(size), float(MIN_SHARES_COUNT))
            
            # 2. Enforce USD Value Minimum ($1.00)
            usd_value = safe_size * price
            if usd_value < float(MIN_TRADE_USD):
                safe_size = float(MIN_TRADE_USD) / price
                safe_size = max(safe_size, float(MIN_SHARES_COUNT))

            # 3. Rounding (Crucial for FOK orders to not fail)
            safe_size = round(safe_size, 2)
            
            logger.info(f"🚀 EXECUTING FOK {side}: {safe_size} shares @ Limit {price:.3f} (${safe_size*price:.2f})")
            
            # --- EXECUTION: FILL OR KILL ---
            order_args = OrderArgs(
                price=price, 
                size=safe_size, 
                side=side, 
                token_id=token_id
            )
            # Sign the order
            signed_order = self.client.create_order(order_args)
            
            # Post as FOK (Immediate fill or cancel)
            # FIX: correct arg is orderType (camelCase)
            resp = self.client.post_order(signed_order, orderType=OrderType.FOK)
            
            logger.info(f"✅ Trade Response: {resp}")
            
            if side == BUY:
                self.total_spent += Decimal(safe_size) * Decimal(price)
            return True

        except Exception as e:
            logger.error(f"Trade Failed (Likely FOK Killed or API Error): {e}")
            return False

    def sync_positions(self):
        """Core Logic: Monitors target, applies Smart Tolerance, executes trades."""
        current_positions = self.get_all_positions(self.target_address)
        
        # Load my positions if needed for selling logic
        if self.my_address:
            try: self.my_positions = self.get_all_positions(self.my_address)
            except: pass

        # Initialization Phase
        if not self.initialized:
            self.target_positions = current_positions
            self.initialized = True
            logger.info(f"📍 Baseline set. Monitoring {len(current_positions)} ACTIVE positions.")
            return

        target_equity = Decimal(0)

        # --- CHECK FOR BUYS (Target Increased Size) ---
        for tid, curr_pos in current_positions.items():
            prev_pos = self.target_positions.get(tid)
            prev_size = prev_pos.size if prev_pos else Decimal(0)
            
            if curr_pos.size > prev_size + POSITION_THRESHOLD:
                delta = curr_pos.size - prev_size
                
                # Lazy load equity only if we are trading
                if target_equity == 0: 
                    target_equity = self.get_portfolio_equity(self.target_address, current_positions)
                
                # Get Target's Entry Price
                target_entry = curr_pos.avg_price
                if target_entry <= 0: target_entry = curr_pos.cur_price

                # Get Current Market Price
                curr_price = curr_pos.cur_price
                
                logger.info(f"🔔 Target INCREASED {curr_pos.market_question[:30]}... by {delta:.2f} @ {target_entry:.2f}")

                # --- ASYMMETRIC SMART TOLERANCE ---
                # Range: [Target - 10c] <----> [Target + 2c]
                
                if target_entry > 0:
                    # 1. UPSIDE CHECK (Don't chase pumps)
                    if curr_price > target_entry + MAX_UPPER_SLIPPAGE:
                         logger.warning(f"⚠️ SKIPPED: Too Expensive (+{curr_price - target_entry:.2f}). Target: {target_entry:.2f}, Market: {curr_price:.2f}")
                         self.target_positions[tid] = curr_pos
                         continue

                    # 2. DOWNSIDE CRASH GUARD (Don't catch falling knives)
                    if curr_price < target_entry - MAX_LOWER_SLIPPAGE:
                         logger.warning(f"⚠️ SKIPPED: Price Crashed (-{target_entry - curr_price:.2f}). Target: {target_entry:.2f}, Market: {curr_price:.2f}. (Toxic flow?)")
                         self.target_positions[tid] = curr_pos
                         continue

                    # 3. POSITIVE SLIPPAGE LOG
                    if curr_price < target_entry:
                        logger.info(f"💎 POSITIVE SLIPPAGE: Buying @ {curr_price:.2f} (Target paid {target_entry:.2f})")

                    # 4. CALCULATE SIZE & EXECUTE
                    my_cash = self.get_my_cash()
                    ratio = (my_cash / target_equity) if target_equity > 0 else 0
                    
                    # Target's spend on this chunk
                    target_spend_usd = delta * target_entry 
                    my_usd_size = target_spend_usd * ratio
                    
                    # Limit Price: Set to Upper Limit to ensure fill, but get best price
                    limit_price = target_entry + MAX_UPPER_SLIPPAGE
                    
                    # Calculate shares based on LIMIT price (conservative) to ensure we have cash
                    if limit_price > 0:
                        shares_to_buy = my_usd_size / limit_price
                        self.place_trade(tid, shares_to_buy, BUY, limit_price=limit_price)
                else:
                    logger.warning("No valid price found, skipping.")

                self.target_positions[tid] = curr_pos

        # --- CHECK FOR SELLS (Target Decreased Size) ---
        # FIX: Iterate over a list of keys to prevent "dictionary changed size" error
        for tid in list(self.target_positions.keys()):
            prev_pos = self.target_positions[tid]
            curr_pos = current_positions.get(tid)
            curr_size = curr_pos.size if curr_pos else Decimal(0)
            
            if curr_size < prev_pos.size - POSITION_THRESHOLD:
                delta = prev_pos.size - curr_size
                logger.info(f"🔔 Target SOLD {prev_pos.market_question[:30]}... ({delta:.2f})")
                
                # Copy Sell Logic: We simply mirror the sell immediately
                if tid in self.my_positions and self.my_positions[tid].size > 0:
                    to_sell = min(delta, self.my_positions[tid].size)
                    
                    price = prev_pos.cur_price
                    if price > 0:
                        # Aggressive limit for SELL to ensure we exit
                        sell_price = price * Decimal("0.90") 
                        self.place_trade(tid, to_sell, SELL, limit_price=sell_price)
                
                if curr_pos: 
                    self.target_positions[tid] = curr_pos
                else: 
                    del self.target_positions[tid]

        # Handle New Positions
        for tid, curr_pos in current_positions.items():
            if tid not in self.target_positions:
                self.target_positions[tid] = curr_pos

        # Heartbeat
        if time.time() - self.last_heartbeat > 10:
            logger.info(f"💓 Scanning... (Tracked: {len(current_positions)})")
            self.last_heartbeat = time.time()

    def run(self):
        logger.info(f"🤖 Bot Started (FOK + SMART TOLERANCE MODE)")
        logger.info(f"Target: {self.target_address}")
        logger.info(f"Slippage: Max +{MAX_UPPER_SLIPPAGE} (Chase) / Max -{MAX_LOWER_SLIPPAGE} (Crash Guard)")
        
        while True:
            try: self.sync_positions()
            except Exception as e: 
                logger.error(f"Loop Error: {e}")
                time.sleep(1)
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    if not PRIVATE_KEY or not TARGET_ADDRESS:
        print("❌ Error: Please set PRIVATE_KEY and TARGET_ADDRESS in .env file")
    else:
        bot = PolymarketMirror(TARGET_ADDRESS, PRIVATE_KEY)
        bot.run()