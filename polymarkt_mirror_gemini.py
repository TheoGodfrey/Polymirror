#!/usr/bin/env python3
"""
Polymarket Mirror Bot (FOK + Smart Tolerances)
- EXECUTION: Fill-Or-Kill (FOK) only. No resting orders.
- SMART COPY: Only follows if price is within X% of target's entry.
- RELIABLE: Solves 'invalid amount' and enforces API minimums.
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

# Libraries
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

# Settings
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

# Risk Management
MAX_CAPITAL_USAGE = Decimal(os.getenv("MAX_CAPITAL_TO_USE", "5000"))
MAX_POSITION_PCT = Decimal("0.20")
MAX_BUY_PRICE = Decimal("0.95")     # Safety Cap

# --- SMART TOLERANCE (THE FIX) ---
# Range: If target buys at 0.50, we buy between 0.49 and 0.51
PRICE_TOLERANCE = Decimal("0.02")   # +/- 2 cents tolerance (adjust as needed)
# ----------------------------------

# Execution Limits
MIN_TRADE_USD = Decimal("1.00")
MIN_SHARES_COUNT = Decimal("5.0")

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
        pos_map = {}
        today_str = datetime.now().strftime("%Y-%m-%d")
        limit = 500 
        offset = 0
        
        while True:
            try:
                params = {"user": address, "limit": limit, "offset": offset} 
                resp = requests.get(f"{DATA_API}/positions", params=params, timeout=5)
                if not resp.ok: raise Exception(f"API Error {resp.status_code}")
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
                raise e
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
        if not self.client: return False
        if self.total_spent >= MAX_CAPITAL_USAGE and side == BUY:
            logger.warning(f"🛑 Max capital limit reached. Skipping.")
            return False

        try:
            price = float(limit_price)
            if price <= 0: return False

            if side == BUY and price > float(MAX_BUY_PRICE):
                logger.info(f"🚫 Price {price:.3f} > Safety Limit. Skipping.")
                return False

            # --- FORCE MINIMUMS ---
            safe_size = max(float(size), float(MIN_SHARES_COUNT))
            usd_value = safe_size * price
            if usd_value < float(MIN_TRADE_USD):
                safe_size = float(MIN_TRADE_USD) / price
                safe_size = max(safe_size, float(MIN_SHARES_COUNT))

            safe_size = round(safe_size, 2)
            
            logger.info(f"🚀 FOK ORDER ({side}): {safe_size} shares @ {price:.3f} (Max/Min allowed)")
            
            # --- FILL OR KILL EXECUTION ---
            # We create the order object, then post it with OrderType.FOK
            order_args = OrderArgs(price=price, size=safe_size, side=side, token_id=token_id)
            signed_order = self.client.create_order(order_args)
            
            # Use FOK to ensure immediate execution or cancel
            resp = self.client.post_order(signed_order, order_type=OrderType.FOK)
            
            logger.info(f"✅ Trade Response: {resp}")
            
            if side == BUY:
                self.total_spent += Decimal(safe_size) * Decimal(price)
            return True

        except Exception as e:
            logger.error(f"Trade Failed (Likely FOK Killed): {e}")
            return False

    def sync_positions(self):
        current_positions = self.get_all_positions(self.target_address)
        
        if self.my_address:
            try: self.my_positions = self.get_all_positions(self.my_address)
            except: pass

        if not self.initialized:
            self.target_positions = current_positions
            self.initialized = True
            logger.info(f"📍 Baseline set. Monitoring {len(current_positions)} ACTIVE positions.")
            return

        target_equity = Decimal(0)

        for tid, curr_pos in current_positions.items():
            prev_pos = self.target_positions.get(tid)
            prev_size = prev_pos.size if prev_pos else Decimal(0)
            
            # --- DETECT BUY ---
            if curr_pos.size > prev_size + POSITION_THRESHOLD:
                delta = curr_pos.size - prev_size
                
                if target_equity == 0: 
                    target_equity = self.get_portfolio_equity(self.target_address, current_positions)
                
                logger.info(f"🔔 Target INCREASED {curr_pos.market_question[:30]}... by {delta:.2f}")
                
                # --- SMART PRICE CHECK ---
                target_entry_price = curr_pos.avg_price
                if target_entry_price <= 0: target_entry_price = curr_pos.cur_price

                current_market_price = curr_pos.cur_price
                
                # Define Tolerances (Target Price +/- Tolerance)
                min_accept = target_entry_price - PRICE_TOLERANCE
                max_accept = target_entry_price + PRICE_TOLERANCE

                if target_entry_price > 0:
                    # Check if market has moved too far
                    if not (min_accept <= current_market_price <= max_accept):
                        logger.warning(f"⚠️ SKIPPED: Price Drift! Target: {target_entry_price:.2f}, Market: {current_market_price:.2f}. Range: {min_accept:.2f}-{max_accept:.2f}")
                        self.target_positions[tid] = curr_pos
                        continue

                    # Calculate Size
                    my_cash = self.get_my_cash()
                    ratio = (my_cash / target_equity) if target_equity > 0 else 0
                    my_usd_size = (delta * target_entry_price) * ratio
                    
                    # For FOK BUY: Set Limit to MAX_ACCEPT to ensure fill if price is within range
                    limit_price = max_accept
                    
                    shares_to_buy = my_usd_size / limit_price
                    self.place_trade(tid, shares_to_buy, BUY, limit_price=limit_price)
                else:
                    logger.warning("No valid price found, skipping.")

                self.target_positions[tid] = curr_pos

        # --- DETECT SELL ---
        for tid, prev_pos in self.target_positions.items():
            curr_pos = current_positions.get(tid)
            curr_size = curr_pos.size if curr_pos else Decimal(0)
            
            if curr_size < prev_pos.size - POSITION_THRESHOLD:
                delta = prev_pos.size - curr_size
                logger.info(f"🔔 Target SOLD {prev_pos.market_question[:30]}... ({delta:.2f})")
                
                if tid in self.my_positions and self.my_positions[tid].size > 0:
                    to_sell = min(delta, self.my_positions[tid].size)
                    
                    # For Sells, we just want out, but we can respect a floor if needed.
                    # Usually, for a copy sell, we dump.
                    price = prev_pos.cur_price
                    if price > 0:
                        sell_price = price * Decimal("0.90") # Aggressive Sell Limit to ensure fill
                        self.place_trade(tid, to_sell, SELL, limit_price=sell_price)
                
                if curr_pos: self.target_positions[tid] = curr_pos
                else: del self.target_positions[tid]

        for tid, curr_pos in current_positions.items():
            if tid not in self.target_positions:
                self.target_positions[tid] = curr_pos

        if time.time() - self.last_heartbeat > 10:
            logger.info(f"💓 Scanning... (Tracked: {len(current_positions)})")
            self.last_heartbeat = time.time()

    def run(self):
        logger.info(f"🤖 Bot Started (FOK + SMART TOLERANCE MODE)")
        logger.info(f"Target: {self.target_address}")
        logger.info(f"Tolerance: +/- {PRICE_TOLERANCE} cents of target execution")
        while True:
            try: self.sync_positions()
            except Exception: time.sleep(0.5) 
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    bot = PolymarketMirror(TARGET_ADDRESS, PRIVATE_KEY)
    bot.run()