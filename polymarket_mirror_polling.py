#!/usr/bin/env python3
"""
Polymarket Mirror Bot v5.1 (Multiplier Edition)
------------------------------------------------
Changes in v5.1:
1. Added SIZE_MULTIPLIER to scale trades relative to target
   (e.g., if target uses 1% of their port, and multiplier is 5, you use 5%)
2. Logic updates in handle_buy and handle_sell to support scaling
"""

import os
import time
import logging
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from dataclasses import dataclass
from datetime import datetime
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

# --- CONFIGURATION ---
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "").lower()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

# Risk Management
MAX_CAPITAL_USAGE = Decimal(os.getenv("MAX_CAPITAL_TO_USE", "5000"))
MAX_BUY_PRICE = Decimal(os.getenv("MAX_BUY_PRICE", "0.95"))
MAX_POSITION_PCT = Decimal(os.getenv("MAX_POSITION_PCT", "0.25"))  # Max 25% per position

# Sizing Multiplier
# 1.0 = Proportional match (They use 1% of their port, you use 1% of yours)
# 5.0 = Aggressive match (They use 1%, you use 5%)
SIZE_MULTIPLIER = Decimal(os.getenv("SIZE_MULTIPLIER", "5.0"))

# Execution Limits
MIN_TRADE_USD = Decimal(os.getenv("MIN_TRADE_USD", "1.00"))
MIN_SHARES = Decimal(os.getenv("MIN_SHARES", "1.0"))

# Speed Settings
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "0.25"))  # 250ms
BALANCE_REFRESH_SEC = int(os.getenv("BALANCE_REFRESH_SEC", "120"))
POSITION_THRESHOLD = Decimal("0.01")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Check imports
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        OrderArgs, OrderType, BalanceAllowanceParams, AssetType
    )
    CLOB_AVAILABLE = True
except ImportError:
    logger.error("py-clob-client not installed: pip install py-clob-client")
    CLOB_AVAILABLE = False


def get_dynamic_slippage(usd_value: Decimal) -> Decimal:
    """
    Dynamic slippage based on position size.
    Smaller positions = more aggressive (higher slippage tolerance)
    Larger positions = more conservative
    """
    if usd_value < 50:
        return Decimal("0.08")   # 8% for tiny positions
    elif usd_value < 150:
        return Decimal("0.06")   # 6%
    elif usd_value < 300:
        return Decimal("0.05")   # 5%
    elif usd_value < 500:
        return Decimal("0.04")   # 4%
    else:
        return Decimal("0.03")   # 3% for large positions


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
    neg_risk: bool = False


@dataclass 
class MarketInfo:
    """Cached market information."""
    tick_size: Decimal
    neg_risk: bool
    fetched_at: float


class PolymarketMirror:
    def __init__(self):
        self.target_address = TARGET_ADDRESS.lower() if TARGET_ADDRESS else ""
        self.my_address = FUNDER_ADDRESS.lower() if FUNDER_ADDRESS else ""
        
        # State
        self.target_positions: dict[str, Position] = {}
        self.my_positions: dict[str, Position] = {}
        self.initialized = False
        self.last_heartbeat = 0
        self.last_poll = 0
        self.total_spent = Decimal(0)

        # Cached balances
        self.cached_target_value = Decimal(0)
        self.cached_my_cash = Decimal(0)
        self.last_balance_update = 0
        
        # Market info cache (token_id -> MarketInfo)
        self.market_cache: dict[str, MarketInfo] = {}
        
        # Performance tracking
        self.trades_attempted = 0
        self.trades_filled = 0
        self.total_slippage = Decimal(0)
        self.total_latency_ms = 0.0

        # Persistent HTTP session
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
        self.session.mount('https://', adapter)

        # CLOB Client
        self.client = None
        if CLOB_AVAILABLE and PRIVATE_KEY:
            self._init_clob_client()

    def _init_clob_client(self):
        """Initialize the CLOB client."""
        try:
            self.client = ClobClient(
                host=POLYMARKET_API,
                chain_id=CHAIN_ID,
                key=PRIVATE_KEY,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER_ADDRESS if FUNDER_ADDRESS else None
            )
            
            # Derive API credentials
            try:
                creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(creds)
                logger.info("✅ Client ready")
            except Exception as e:
                logger.warning(f"API creds warning: {e}")
            
        except Exception as e:
            logger.error(f"❌ Client init failed: {e}")
            self.client = None

    def get_cash_balance(self) -> Decimal:
        """Get USDC balance."""
        if not self.client:
            return Decimal(0)
        
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self.client.get_balance_allowance(params)
            if result and isinstance(result, dict):
                balance_raw = result.get("balance", "0")
                return Decimal(str(balance_raw)) / Decimal("1000000")
        except Exception as e:
            logger.debug(f"Balance error: {e}")
        
        return Decimal(0)

    def get_positions(self, address: str) -> dict[str, Position]:
        """Fetch positions for an address."""
        positions = {}
        today = datetime.now().strftime("%Y-%m-%d")
        
        try:
            offset = 0
            limit = 500
            
            while True:
                resp = self.session.get(
                    f"{DATA_API}/positions",
                    params={"user": address, "limit": limit, "offset": offset},
                    timeout=5
                )
                
                if not resp.ok:
                    break
                
                data = resp.json()
                if not data:
                    break
                
                for p in data:
                    size = Decimal(str(p.get("size", 0)))
                    if size < Decimal("0.001"):
                        continue
                    
                    end_date = p.get("endDate", "")
                    if end_date and end_date < today:
                        continue
                    
                    token_id = p.get("asset") or p.get("asset_id") or p.get("tokenId")
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
                        market_question=p.get("title") or p.get("question", "Unknown"),
                        end_date=end_date,
                        neg_risk=p.get("negRisk", False)
                    )
                
                if len(data) < limit:
                    break
                offset += limit
                
        except Exception as e:
            logger.error(f"Position fetch error: {e}")
        
        return positions

    def refresh_state(self):
        """Update cached balances. Call AFTER positions are loaded."""
        self.cached_my_cash = self.get_cash_balance()
        self.cached_target_value = sum(
            p.size * (p.cur_price if p.cur_price > 0 else p.avg_price)
            for p in self.target_positions.values()
        )
        self.last_balance_update = time.time()
        logger.info(f"💰 Cash: ${self.cached_my_cash:.2f} | Target Positions: ${self.cached_target_value:.2f}")

    def get_market_info(self, token_id: str) -> MarketInfo:
        """Get market info with caching."""
        # Check cache
        cached = self.market_cache.get(token_id)
        if cached and time.time() - cached.fetched_at < 300:
            return cached
        
        # Fetch fresh
        tick_size = Decimal("0.01")
        neg_risk = False
        
        try:
            resp = self.session.get(
                f"{DATA_API}/markets",
                params={"clob_token_ids": token_id},
                timeout=3
            )
            if resp.ok:
                markets = resp.json()
                if markets:
                    m = markets[0]
                    tick_size = Decimal(str(m.get("minimum_tick_size", "0.01")))
                    neg_risk = m.get("neg_risk", False)
        except:
            pass
        
        info = MarketInfo(tick_size=tick_size, neg_risk=neg_risk, fetched_at=time.time())
        self.market_cache[token_id] = info
        return info

    def place_order(self, token_id: str, side: str, size: Decimal, 
                    limit_price: Decimal, target_price: Decimal,
                    market_question: str = "") -> bool:
        """Place an order."""
        start_time = time.time()
        
        if not self.client:
            logger.info(f"[DRY RUN] {side} {size:.2f} @ {limit_price:.3f}")
            return False
        
        # Validate size
        if size < MIN_SHARES:
            return False
        
        usd_value = size * limit_price
        if usd_value < MIN_TRADE_USD:
            return False
        
        # Capital check for buys
        if side == BUY:
            if limit_price > MAX_BUY_PRICE:
                logger.warning(f"Price {limit_price:.3f} > max {MAX_BUY_PRICE}")
                return False
            
            if self.total_spent + usd_value > MAX_CAPITAL_USAGE:
                logger.warning(f"Capital limit: ${self.total_spent:.0f} + ${usd_value:.0f} > ${MAX_CAPITAL_USAGE}")
                return False
        
        self.trades_attempted += 1
        
        try:
            # Get market info
            info = self.get_market_info(token_id)
            
            # Round price to tick size
            if side == BUY:
                rounded_price = (limit_price / info.tick_size).quantize(Decimal("1"), rounding=ROUND_UP) * info.tick_size
            else:
                rounded_price = (limit_price / info.tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * info.tick_size
            
            rounded_size = size.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            
            logger.info(f"⚡ {side} {rounded_size:.1f} @ {rounded_price:.3f} | {market_question[:35]}...")
            
            order_args = OrderArgs(
                token_id=token_id,
                price=float(rounded_price),
                size=float(rounded_size),
                side=BUY if side == BUY else SELL
            )
            
            signed_order = self.client.create_order(order_args, options={"neg_risk": info.neg_risk})
            resp = self.client.post_order(signed_order, order_type=OrderType.FOK)
            
            latency_ms = (time.time() - start_time) * 1000
            
            if resp:
                success = resp.get("success", False) if isinstance(resp, dict) else getattr(resp, "success", False)
                if success:
                    self.trades_filled += 1
                    self.total_latency_ms += latency_ms
                    
                    # Calculate slippage
                    if target_price > 0:
                        if side == BUY:
                            slippage = rounded_price - target_price
                        else:
                            slippage = target_price - rounded_price
                        slippage_pct = (slippage / target_price) * 100
                        self.total_slippage += slippage
                        
                        logger.info(f"✅ Filled {latency_ms:.0f}ms | Target {target_price:.3f} → Us {rounded_price:.3f} ({slippage_pct:+.1f}%)")
                    else:
                        logger.info(f"✅ Filled {latency_ms:.0f}ms @ {rounded_price:.3f}")
                    
                    if side == BUY:
                        cost = rounded_size * rounded_price
                        self.total_spent += cost
                        self.cached_my_cash -= cost
                    
                    return True
                else:
                    error = resp.get("errorMsg", str(resp)) if isinstance(resp, dict) else str(resp)
                    logger.warning(f"❌ Rejected: {error[:60]}")
            
            return False
            
        except Exception as e:
            logger.error(f"Order error: {e}")
            return False

    def handle_buy(self, token_id: str, position: Position, delta: Decimal):
        """Handle detected buy signal with MULTIPLIER logic."""
        target_entry = position.avg_price if position.avg_price > 0 else position.cur_price
        cur_price = position.cur_price
        
        if target_entry <= 0:
            logger.warning(f"Invalid price for {position.market_question[:30]}")
            return
        
        # --- CALCULATE RATIO WITH MULTIPLIER ---
        # Ratio = (MyCash / TargetPortfolio) * Multiplier
        if self.cached_target_value > 0:
            ratio = (self.cached_my_cash / self.cached_target_value) * SIZE_MULTIPLIER
        else:
            ratio = Decimal("0.01") * SIZE_MULTIPLIER
        
        target_usd = delta * target_entry
        my_usd = target_usd * ratio
        
        # Cap at max position %
        max_pos = self.cached_my_cash * MAX_POSITION_PCT
        my_usd = min(my_usd, max_pos)
        
        # Skip if truly tiny (under 55c), otherwise minimum $1
        SKIP_THRESHOLD = Decimal("0.25")
        if my_usd < SKIP_THRESHOLD:
            logger.info(f"   ⏭️ Skip: ${my_usd:.2f} < ${SKIP_THRESHOLD} threshold")
            return
        
        # Round up to minimum $1 if between 30c and $1
        if my_usd < MIN_TRADE_USD:
            logger.info(f"   📈 Rounding ${my_usd:.2f} → ${MIN_TRADE_USD}")
            my_usd = MIN_TRADE_USD
        
        # Dynamic slippage
        slippage = get_dynamic_slippage(my_usd)
        limit_price = min(target_entry * (Decimal("1") + slippage), MAX_BUY_PRICE)
        
        # Check current price isn't crazy
        if cur_price > 0 and cur_price > limit_price:
            logger.warning(f"   ⏭️ Skip: Price moved {cur_price:.3f} > limit {limit_price:.3f}")
            return
        
        shares = my_usd / limit_price
        
        logger.info(f"   📐 Ratio: {ratio:.4f} (Mult: {SIZE_MULTIPLIER}x) | Target Buy: ${target_usd:.0f} → My Buy: ${my_usd:.2f}")

        self.place_order(
            token_id, BUY, shares, limit_price, target_entry,
            position.market_question
        )

    def handle_sell(self, token_id: str, position: Position, delta: Decimal):
        """Handle detected sell signal with MULTIPLIER logic."""
        # Check if we own it
        if token_id not in self.my_positions:
            self.my_positions = self.get_positions(self.my_address)
        
        my_pos = self.my_positions.get(token_id)
        if not my_pos or my_pos.size <= 0:
            return
        
        # --- SELL LOGIC WITH MULTIPLIER ---
        # If we bought 5x, we need to sell 5x
        # We calculate the sell amount based on the multiplier
        scaled_delta = delta * SIZE_MULTIPLIER
        
        # We can't sell more than we have
        to_sell = min(scaled_delta, my_pos.size)
        
        target_exit = position.cur_price if position.cur_price > 0 else position.avg_price
        
        # Sell slightly below for faster fill
        sell_price = target_exit * Decimal("0.95")
        
        if sell_price > 0:
            logger.info(f"   📉 Mirror Sell: Target sold {delta:.1f} → I sell {to_sell:.1f} (Mult: {SIZE_MULTIPLIER}x)")
            self.place_order(
                token_id, SELL, to_sell, sell_price, target_exit,
                position.market_question
            )

    def poll_once(self):
        """Single poll iteration."""
        current = self.get_positions(self.target_address)
        
        # First run - initialize
        if not self.initialized:
            self.target_positions = current
            self.my_positions = self.get_positions(self.my_address)
            self.initialized = True
            self.refresh_state()  # AFTER positions loaded
            logger.info(f"📍 Tracking {len(current)} positions | Multiplier: {SIZE_MULTIPLIER}x")
            self.last_poll = time.time()
            return
        
        # Refresh balances periodically
        if time.time() - self.last_balance_update > BALANCE_REFRESH_SEC:
            self.refresh_state()
        
        # Detect BUYs
        for tid, pos in current.items():
            prev = self.target_positions.get(tid)
            prev_size = prev.size if prev else Decimal(0)
            
            if pos.size > prev_size + POSITION_THRESHOLD:
                delta = pos.size - prev_size
                logger.info(f"🔔 BUY: {pos.market_question[:40]} (+{delta:.1f} @ {pos.avg_price:.3f})")
                self.handle_buy(tid, pos, delta)
                self.target_positions[tid] = pos
        
        # Detect SELLs
        for tid in list(self.target_positions.keys()):
            prev = self.target_positions[tid]
            curr = current.get(tid)
            curr_size = curr.size if curr else Decimal(0)
            
            if curr_size < prev.size - POSITION_THRESHOLD:
                delta = prev.size - curr_size
                logger.info(f"🔔 SELL: {prev.market_question[:40]} (-{delta:.1f})")
                self.handle_sell(tid, prev, delta)
                
                if curr:
                    self.target_positions[tid] = curr
                else:
                    del self.target_positions[tid]
        
        # Track new positions
        for tid, pos in current.items():
            if tid not in self.target_positions:
                self.target_positions[tid] = pos
        
        self.last_poll = time.time()
        
        # Heartbeat
        if time.time() - self.last_heartbeat > 10:
            logger.info(f"💓 {len(self.target_positions)} pos | Mult: {SIZE_MULTIPLIER}x | Cash: ${self.cached_my_cash:.0f}")
            self.last_heartbeat = time.time()

    def run(self):
        """Main loop."""
        logger.info("=" * 50)
        logger.info("⚡ Polymarket Mirror v5.1 (Multiplier)")
        logger.info("=" * 50)
        logger.info(f"Target: {self.target_address}")
        logger.info(f"Multiplier: {SIZE_MULTIPLIER}x")
        logger.info(f"Poll: {POLL_INTERVAL}s")
        logger.info("=" * 50)
        
        try:
            while True:
                try:
                    self.poll_once()
                except Exception as e:
                    logger.error(f"Error: {e}")
                
                time.sleep(POLL_INTERVAL)
                
        except KeyboardInterrupt:
            logger.info("\n⏹️ Stopped")


if __name__ == "__main__":
    if not PRIVATE_KEY:
        print("❌ PRIVATE_KEY not set")
        exit(1)
    if not TARGET_ADDRESS:
        print("❌ TARGET_ADDRESS not set")
        exit(1)
    if not FUNDER_ADDRESS:
        print("❌ FUNDER_ADDRESS not set")
        exit(1)
    
    bot = PolymarketMirror()
    bot.run()