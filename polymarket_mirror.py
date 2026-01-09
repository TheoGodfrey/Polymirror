#!/usr/bin/env python3
"""
Polymarket Mirror Trading Bot (Fixed & Optimized)
Monitors a target account and mirrors their positions in real-time.
"""

import os
import time
import json
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from typing import Optional
from dataclasses import dataclass
from datetime import datetime
from dotenv import load_dotenv
import requests

# --- CONSTANTS & CONFIG ---
BUY = "BUY"
SELL = "SELL"
CHAIN_ID = 137
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI = '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"}]'

# Websockets
try:
    import websockets
    from websockets.exceptions import ConnectionClosed as WSConnectionClosed
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    WSConnectionClosed = Exception

# Web3
try:
    from web3 import Web3
    from eth_account import Account
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

# Polymarket CLOB
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    print("Warning: py-clob-client not installed.")

load_dotenv()

# Config
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")

# API Endpoints
POLYMARKET_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
RPC_URL = "https://polygon-rpc.com" # Public RPC

# Settings
POLL_INTERVAL = 1.0           # 1s for High Frequency
POSITION_THRESHOLD = Decimal("0.1")
SIZING_MODE = os.getenv("SIZING_MODE", "proportional")
MAX_POSITION_PCT = Decimal(os.getenv("MAX_POSITION_PCT", "0.10"))
MAX_POSITION_USDC = Decimal(os.getenv("MAX_POSITION_USDC", "100"))
MIN_POSITION_USDC = Decimal(os.getenv("MIN_POSITION_USDC", "1.00"))

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
    market_question: str

class PolymarketMirror:
    def __init__(self, target_address: str, private_key: str):
        self.target_address = target_address.lower() if target_address else ""
        self.private_key = private_key
        self.target_positions: dict[str, Position] = {} # Stores LAST known state
        self.initialized = False # Flag for baseline sync
        
        # Web3 Setup
        if WEB3_AVAILABLE:
            self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
            self.usdc_contract = self.w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
        else:
            self.w3 = None
            logger.warning("Web3 not available. Balance checks will be less accurate.")

        # Client Setup
        if CLOB_AVAILABLE and private_key:
            try:
                if not FUNDER_ADDRESS:
                    funder = Account.from_key(private_key).address
                else:
                    funder = FUNDER_ADDRESS
                
                self.client = ClobClient(
                    POLYMARKET_API,
                    key=private_key,
                    chain_id=CHAIN_ID,
                    signature_type=SIGNATURE_TYPE,
                    funder=funder
                )
                
                # Credentials
                try:
                    creds = self.client.derive_api_key()
                    self.client.set_api_creds(creds)
                    logger.info("Credentials derived successfully.")
                except Exception:
                    logger.info("Could not derive credentials, attempting creation (or already set).")
            except Exception as e:
                logger.error(f"Client init error: {e}")
                self.client = None
        else:
            self.client = None

    def get_usdc_balance_web3(self, address: str) -> Decimal:
        """Fetch reliable USDC balance from chain."""
        if not self.w3: return Decimal(0)
        try:
            raw = self.usdc_contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
            return Decimal(str(raw)) / Decimal("1000000")
        except Exception as e:
            logger.error(f"Web3 Balance Error: {e}")
            return Decimal(0)

    def get_account_positions(self, address: str) -> dict[str, Position]:
        """Fetch current positions from Data API."""
        pos_map = {}
        try:
            resp = requests.get(f"{DATA_API}/positions", params={"user": address}, timeout=5)
            if not resp.ok: return {}
            data = resp.json()
            if not isinstance(data, list): return {}
            
            for p in data:
                size = Decimal(str(p.get("size", 0)))
                if size < POSITION_THRESHOLD: continue
                
                tid = p.get("asset_id") or p.get("tokenId") # Handle API variations
                if not tid: continue
                
                pos_map[tid] = Position(
                    market_id=p.get("marketId", ""),
                    token_id=tid,
                    outcome=p.get("outcome", ""),
                    size=size,
                    avg_price=Decimal(str(p.get("avgPrice", 0))),
                    market_question=p.get("question", "Unknown")
                )
        except Exception as e:
            logger.error(f"API Error: {e}")
        return pos_map

    def get_portfolio_equity(self, address: str, positions: dict = None) -> Decimal:
        """Calculate total equity (Cash + Position Value)."""
        # 1. Cash
        cash = self.get_usdc_balance_web3(address)
        
        # 2. Positions
        if positions is None:
            positions = self.get_account_positions(address)
            
        pos_val = Decimal(0)
        for p in positions.values():
            # Use avgPrice as proxy for current value if live price unavailable
            pos_val += p.size * p.avg_price
            
        return cash + pos_val

    def get_my_cash(self) -> Decimal:
        """Get my available trading cash."""
        if self.client:
            try:
                res = self.client.get_balance_allowance(asset_type="COLLATERAL")
                return Decimal(res['balance']) / Decimal("1000000")
            except:
                pass
        return self.get_usdc_balance_web3(Account.from_key(self.private_key).address)

    def calculate_buy_size(self, target_delta: Decimal, target_equity: Decimal, price: Decimal) -> Decimal:
        """Calculate buy size based on Proportional Equity."""
        if target_equity <= 0: return Decimal(0)
        
        my_cash = self.get_my_cash()
        
        # Ratio: My Cash vs Target Total Equity
        # (This is conservative: we assume our Cash is our "Portfolio" to allocate)
        ratio = my_cash / target_equity
        
        # Cap Ratio (Max 10% of our portfolio per trade)
        ratio = min(ratio, Decimal("0.10"))
        
        # Target bought 'target_delta'. We buy 'target_delta * ratio'
        my_size = target_delta * ratio
        
        # Caps
        usd_val = my_size * price
        if usd_val > MAX_POSITION_USDC:
            my_size = MAX_POSITION_USDC / price
            
        return my_size

    def place_trade(self, token_id: str, size: Decimal, side: str):
        if not self.client: return
        
        try:
            # 1. Get Orderbook for Price
            book = self.client.get_order_book(token_id)
            if side == BUY:
                if not book.asks: return
                price = float(book.asks[0].price)
            else:
                if not book.bids: return
                price = float(book.bids[0].price)
                
            # 2. Rounding
            size = round(float(size), 2)
            if size <= 0: return

            logger.info(f"🚀 EXECUTING {side}: {size} shares @ {price}")
            
            order_args = OrderArgs(
                price=price,
                size=size,
                side=side,
                token_id=token_id,
            )
            resp = self.client.create_order(order_args)
            logger.info(f"✅ Trade Response: {resp}")
            
        except Exception as e:
            logger.error(f"Trade Execution Failed: {e}")

    def sync_positions(self):
        """
        Delta-based Sync:
        1. Fetch Current Target Positions.
        2. Compare with Last Known State.
        3. If NEW > OLD, Buy.
        """
        
        # 1. Fetch Current
        current_positions = self.get_account_positions(self.target_address)
        
        # 2. Baseline Check (First Run)
        if not self.initialized:
            self.target_positions = current_positions
            self.initialized = True
            logger.info(f"📍 Baseline established. Monitoring {len(current_positions)} existing positions (No trades).")
            return

        # 3. Calculate Equity for Sizing (Only if we find a trade, to save RPC calls? 
        #    Actually better to fetch once per loop or lazy load)
        target_equity = Decimal(0) # Lazy load
        
        # 4. Check for BUYS (Increases)
        for tid, curr_pos in current_positions.items():
            prev_pos = self.target_positions.get(tid)
            prev_size = prev_pos.size if prev_pos else Decimal(0)
            
            if curr_pos.size > prev_size + POSITION_THRESHOLD:
                # --- NEW BUY DETECTED ---
                delta = curr_pos.size - prev_size
                
                # Fetch equity now needed
                if target_equity == 0:
                    target_equity = self.get_portfolio_equity(self.target_address, current_positions)
                
                logger.info(f"🔔 Target INCREASED {curr_pos.market_question[:30]}... by {delta:.2f}")
                
                # Calculate Size
                price = curr_pos.avg_price # Use avg as estimate
                my_size = self.calculate_buy_size(delta, target_equity, price)
                
                usd_value = my_size * price
                if usd_value >= MIN_POSITION_USDC:
                    self.place_trade(tid, my_size, BUY)
                else:
                    logger.info(f"Skipping dust trade (${usd_value:.2f})")

        # 5. Check for SELLS (Decreases) - Optional, but good for mirroring
        for tid, prev_pos in self.target_positions.items():
            curr_pos = current_positions.get(tid)
            curr_size = curr_pos.size if curr_pos else Decimal(0)
            
            if curr_size < prev_pos.size - POSITION_THRESHOLD:
                # --- SELL DETECTED ---
                delta = prev_pos.size - curr_size
                logger.info(f"🔔 Target SOLD {prev_pos.market_question[:30]}... ({delta:.2f})")
                
                # We simply sell proportional to what WE have? 
                # Or try to mirror the sell. For simplicity: Sell equivalent ratio if we have it.
                # For now, let's just log it or implement simple sell.
                self.place_trade(tid, delta, SELL) # Attempt to sell delta (might fail if we don't have it)

        # 6. Update State
        self.target_positions = current_positions

    def run(self):
        logger.info(f"🤖 Bot Started. Target: {self.target_address}")
        logger.info(f"⚡ Mode: Polling ({POLL_INTERVAL}s)")
        
        while True:
            try:
                self.sync_positions()
            except Exception as e:
                logger.error(f"Loop Error: {e}")
                time.sleep(1)
            
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    bot = PolymarketMirror(TARGET_ADDRESS, PRIVATE_KEY)
    bot.run()