#!/usr/bin/env python3
"""
Polymarket Mirror Bot v2 (Speed Edition - Fixed)
------------------------------------------------
Fixes from original:
1. Balance fetching uses correct BalanceAllowanceParams
2. Portfolio calculation fixed
3. Order creation includes tick_size and neg_risk
4. Better error handling
5. Proper API credential derivation
"""

import os
import time
import logging
from decimal import Decimal, ROUND_DOWN
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
MAX_POSITION_PCT = Decimal(os.getenv("MAX_POSITION_PCT", "0.20"))  # Max 20% per position

# Slippage Tolerance
MAX_UPPER_SLIPPAGE = Decimal(os.getenv("MAX_UPPER_SLIPPAGE", "0.04"))
MAX_LOWER_SLIPPAGE = Decimal(os.getenv("MAX_LOWER_SLIPPAGE", "0.10"))

# Execution Limits
MIN_TRADE_USD = Decimal(os.getenv("MIN_TRADE_USD", "1.00"))
MIN_SHARES = Decimal(os.getenv("MIN_SHARES", "1.0"))

# Speed Settings
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "0.5"))  # 500ms default
BALANCE_REFRESH_SEC = int(os.getenv("BALANCE_REFRESH_SEC", "60"))
POSITION_THRESHOLD = Decimal("0.01")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
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
class TradeStats:
    """Track performance metrics for each mirrored trade."""
    token_id: str
    market_question: str
    target_price: Decimal      # Price target got
    our_price: Decimal         # Price we got
    price_slippage: Decimal    # Difference (negative = we paid more)
    detection_time: float      # When we detected the trade
    execution_time: float      # When our order filled
    latency_ms: float          # Time from detection to fill
    side: str
    size: Decimal
    success: bool


class PolymarketMirror:
    def __init__(self, target_address: str, private_key: str):
        self.target_address = target_address.lower() if target_address else ""
        self.private_key = private_key
        self.my_address = FUNDER_ADDRESS.lower() if FUNDER_ADDRESS else ""
        
        # State
        self.target_positions: dict[str, Position] = {}
        self.my_positions: dict[str, Position] = {}
        self.initialized = False
        self.last_heartbeat = 0
        self.total_spent = Decimal(0)

        # Cached balances (updated every BALANCE_REFRESH_SEC)
        self.cached_target_equity = Decimal(0)
        self.cached_my_cash = Decimal(0)
        self.last_balance_update = 0
        
        # Performance tracking
        self.trade_stats: list[TradeStats] = []
        self.total_slippage = Decimal(0)
        self.total_latency_ms = 0.0
        self.trades_attempted = 0
        self.trades_filled = 0

        # Persistent HTTP session for speed
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retries))

        # CLOB Client
        self.client = None
        if CLOB_AVAILABLE and private_key:
            self._init_clob_client()

    def _init_clob_client(self):
        """Initialize the CLOB client with proper credentials."""
        try:
            funder = FUNDER_ADDRESS if FUNDER_ADDRESS else None
            
            self.client = ClobClient(
                host=POLYMARKET_API,
                chain_id=CHAIN_ID,
                key=self.private_key,
                signature_type=SIGNATURE_TYPE,
                funder=funder
            )
            
            # Derive API credentials
            try:
                creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(creds)
                logger.info("✅ API credentials derived")
            except Exception as e:
                logger.warning(f"Could not derive API creds: {e}")
            
            # Get our address
            if funder:
                self.my_address = funder.lower()
            
            logger.info(f"✅ CLOB Client initialized")
            logger.info(f"   My address: {self.my_address}")
            logger.info(f"   Target: {self.target_address}")
            
        except Exception as e:
            logger.error(f"❌ CLOB init failed: {e}")
            self.client = None

    def get_my_cash_balance(self) -> Decimal:
        """Get our USDC balance using CLOB API."""
        if not self.client:
            return Decimal(0)
        
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self.client.get_balance_allowance(params)
            if result and isinstance(result, dict):
                balance_raw = result.get("balance", "0")
                # Convert from micro-units (6 decimals)
                return Decimal(str(balance_raw)) / Decimal("1000000")
        except Exception as e:
            logger.debug(f"Balance fetch error: {e}")
        
        return Decimal(0)

    def get_positions(self, address: str) -> dict[str, Position]:
        """Fetch all active positions for an address."""
        positions = {}
        today = datetime.now().strftime("%Y-%m-%d")
        
        try:
            # Fetch with pagination
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
                    
                    # Skip expired markets
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
            logger.error(f"Error fetching positions for {address[:10]}: {e}")
        
        return positions

    def calculate_portfolio_value(self, positions: dict[str, Position], cash: Decimal = Decimal(0)) -> Decimal:
        """Calculate total portfolio value from positions + cash."""
        pos_value = sum(
            p.size * (p.cur_price if p.cur_price > 0 else p.avg_price)
            for p in positions.values()
        )
        return pos_value + cash

    def refresh_balances(self):
        """Update cached balance values (called periodically)."""
        try:
            # My cash balance
            self.cached_my_cash = self.get_my_cash_balance()
            
            # Target equity = their position values
            # (we track positions separately, so equity = sum of position values)
            self.cached_target_equity = self.calculate_portfolio_value(self.target_positions)
            
            self.last_balance_update = time.time()
            
            logger.info(f"💰 Balances: My Cash=${self.cached_my_cash:.2f} | Target Equity=${self.cached_target_equity:.2f}")
            
        except Exception as e:
            logger.error(f"Balance refresh failed: {e}")

    def get_market_info(self, token_id: str) -> tuple[Decimal, str, bool]:
        """Get current price, tick_size, and neg_risk for a token."""
        try:
            # Get price from order book
            book = self.client.get_order_book(token_id)
            if book:
                # Best ask for buying, best bid for selling
                if book.asks:
                    price = Decimal(str(book.asks[0].price))
                elif book.bids:
                    price = Decimal(str(book.bids[0].price))
                else:
                    price = Decimal(0)
            else:
                price = Decimal(0)
            
            # Get market details for tick_size and neg_risk
            # Default values if we can't fetch
            tick_size = "0.01"
            neg_risk = False
            
            try:
                # Try to get from market endpoint
                resp = self.session.get(
                    f"{DATA_API}/markets",
                    params={"clob_token_ids": token_id},
                    timeout=3
                )
                if resp.ok:
                    markets = resp.json()
                    if markets and len(markets) > 0:
                        m = markets[0]
                        tick_size = m.get("minimum_tick_size", "0.01")
                        neg_risk = m.get("neg_risk", False)
            except:
                pass
            
            return price, tick_size, neg_risk
            
        except Exception as e:
            logger.debug(f"Market info error: {e}")
            return Decimal(0), "0.01", False

    def place_order(self, token_id: str, side: str, size: Decimal, price: Decimal, 
                    neg_risk: bool = False, target_price: Decimal = None, 
                    market_question: str = "") -> tuple[bool, Decimal, float]:
        """
        Place an order with proper parameters.
        Returns: (success, fill_price, latency_ms)
        """
        start_time = time.time()
        
        if not self.client:
            logger.info(f"[DRY RUN] {side} {size:.2f} @ {price:.3f}")
            return False, Decimal(0), 0.0
        
        # Validate
        if size < MIN_SHARES:
            logger.debug(f"Size too small: {size} < {MIN_SHARES}")
            return False, Decimal(0), 0.0
        
        usd_value = size * price
        if usd_value < MIN_TRADE_USD:
            logger.debug(f"Value too small: ${usd_value:.2f} < ${MIN_TRADE_USD}")
            return False, Decimal(0), 0.0
        
        if side == BUY:
            if price > MAX_BUY_PRICE:
                logger.warning(f"Price too high: {price:.3f} > {MAX_BUY_PRICE}")
                return False, Decimal(0), 0.0
            
            if self.total_spent + usd_value > MAX_CAPITAL_USAGE:
                logger.warning(f"Capital limit reached: ${self.total_spent:.2f} + ${usd_value:.2f} > ${MAX_CAPITAL_USAGE}")
                return False, Decimal(0), 0.0
        
        self.trades_attempted += 1
        
        try:
            # Get tick_size for this market
            _, tick_size, market_neg_risk = self.get_market_info(token_id)
            use_neg_risk = neg_risk or market_neg_risk
            
            # Round price to tick size
            tick = Decimal(tick_size)
            rounded_price = float((price / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick)
            rounded_size = float(size.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
            
            logger.info(f"🚀 {side}: {rounded_size:.2f} shares @ {rounded_price:.3f} (neg_risk={use_neg_risk})")
            
            order_args = OrderArgs(
                token_id=token_id,
                price=rounded_price,
                size=rounded_size,
                side=BUY if side == "BUY" else SELL
            )
            
            # Create and post order
            signed_order = self.client.create_order(order_args, options={"neg_risk": use_neg_risk})
            resp = self.client.post_order(signed_order, order_type=OrderType.FOK)
            
            latency_ms = (time.time() - start_time) * 1000
            
            if resp:
                success = resp.get("success", False) if isinstance(resp, dict) else getattr(resp, "success", False)
                if success:
                    fill_price = Decimal(str(rounded_price))
                    self.trades_filled += 1
                    self.total_latency_ms += latency_ms
                    
                    # Calculate slippage vs target
                    if target_price and target_price > 0:
                        if side == BUY:
                            slippage = fill_price - target_price  # Positive = we paid more
                        else:
                            slippage = target_price - fill_price  # Positive = we got less
                        slippage_pct = (slippage / target_price) * 100
                        self.total_slippage += slippage
                        
                        # Log with lag info
                        logger.info(f"✅ FILLED in {latency_ms:.0f}ms | "
                                   f"Target: {target_price:.3f} → Us: {fill_price:.3f} | "
                                   f"Slippage: {slippage:+.4f} ({slippage_pct:+.2f}%)")
                        
                        # Store stats
                        self.trade_stats.append(TradeStats(
                            token_id=token_id,
                            market_question=market_question[:50],
                            target_price=target_price,
                            our_price=fill_price,
                            price_slippage=slippage,
                            detection_time=start_time,
                            execution_time=time.time(),
                            latency_ms=latency_ms,
                            side=side,
                            size=Decimal(str(rounded_size)),
                            success=True
                        ))
                    else:
                        logger.info(f"✅ FILLED in {latency_ms:.0f}ms @ {fill_price:.3f}")
                    
                    if side == BUY:
                        self.total_spent += Decimal(str(rounded_size)) * Decimal(str(rounded_price))
                        self.cached_my_cash -= Decimal(str(rounded_size)) * Decimal(str(rounded_price))
                    
                    return True, fill_price, latency_ms
                else:
                    error = resp.get("errorMsg", resp) if isinstance(resp, dict) else getattr(resp, "errorMsg", str(resp))
                    logger.warning(f"❌ Order rejected ({latency_ms:.0f}ms): {error}")
            
            return False, Decimal(0), latency_ms
            
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            logger.error(f"Order error ({latency_ms:.0f}ms): {e}")
            return False, Decimal(0), latency_ms

    def process_buy_signal(self, token_id: str, position: Position, delta: Decimal):
        """Process a detected buy signal."""
        target_entry = position.avg_price if position.avg_price > 0 else position.cur_price
        cur_price = position.cur_price
        
        if target_entry <= 0 or cur_price <= 0:
            logger.warning(f"Invalid prices for {position.market_question[:30]}")
            return
        
        # Slippage checks
        if cur_price > target_entry + MAX_UPPER_SLIPPAGE:
            logger.warning(f"⚠️ Price moved up too much: {cur_price:.3f} > {target_entry:.3f} + {MAX_UPPER_SLIPPAGE}")
            return
        
        if cur_price < target_entry - MAX_LOWER_SLIPPAGE:
            logger.warning(f"⚠️ Price crashed: {cur_price:.3f} < {target_entry:.3f} - {MAX_LOWER_SLIPPAGE}")
            return
        
        # Calculate position size (proportional to portfolio)
        if self.cached_target_equity > 0:
            ratio = self.cached_my_cash / self.cached_target_equity
        else:
            ratio = Decimal("0.01")  # Fallback 1%
        
        target_usd = delta * target_entry
        my_usd = target_usd * ratio
        
        # Cap at max position percentage
        max_position = self.cached_my_cash * MAX_POSITION_PCT
        my_usd = min(my_usd, max_position)
        
        if my_usd < MIN_TRADE_USD:
            logger.debug(f"Position too small: ${my_usd:.2f}")
            return
        
        # Calculate shares at limit price (allow some slippage)
        limit_price = min(target_entry + MAX_UPPER_SLIPPAGE, MAX_BUY_PRICE)
        shares = my_usd / limit_price
        
        self.place_order(
            token_id, BUY, shares, limit_price, 
            position.neg_risk, 
            target_price=target_entry,
            market_question=position.market_question
        )

    def process_sell_signal(self, token_id: str, position: Position, delta: Decimal):
        """Process a detected sell signal."""
        # Check if we own this position
        if token_id not in self.my_positions:
            # Refresh my positions
            self.my_positions = self.get_positions(self.my_address)
        
        my_pos = self.my_positions.get(token_id)
        if not my_pos or my_pos.size <= 0:
            logger.debug(f"We don't own {position.market_question[:30]}")
            return
        
        # Sell proportionally or all
        to_sell = min(delta, my_pos.size)
        
        # Target's exit price
        target_exit = position.cur_price if position.cur_price > 0 else position.avg_price
        
        # Sell at slightly below current price for faster fill
        sell_price = target_exit * Decimal("0.95")
        
        if sell_price > 0:
            self.place_order(
                token_id, SELL, to_sell, sell_price, 
                position.neg_risk,
                target_price=target_exit,
                market_question=position.market_question
            )

    def sync_positions(self):
        """Main sync loop - detect and mirror trades."""
        # Refresh balances periodically
        if time.time() - self.last_balance_update > BALANCE_REFRESH_SEC:
            self.refresh_balances()
        
        # Fetch current target positions
        current_positions = self.get_positions(self.target_address)
        
        # First run - just store state
        if not self.initialized:
            self.target_positions = current_positions
            self.refresh_balances()
            self.initialized = True
            
            total_value = self.calculate_portfolio_value(current_positions)
            logger.info(f"📍 Initialized. Tracking {len(current_positions)} positions (${total_value:.2f})")
            logger.info(f"   My cash: ${self.cached_my_cash:.2f}")
            
            # Show sizing info
            if total_value > 0:
                ratio = self.cached_my_cash / total_value
                min_target_trade = MIN_TRADE_USD / ratio if ratio > 0 else float('inf')
                logger.info(f"   Sizing ratio: {ratio:.4f} ({ratio*100:.2f}%)")
                logger.info(f"   Min target trade to mirror: ${min_target_trade:.2f}")
            return
        
        # Detect BUYs (new or increased positions)
        for token_id, curr_pos in current_positions.items():
            prev_pos = self.target_positions.get(token_id)
            prev_size = prev_pos.size if prev_pos else Decimal(0)
            
            if curr_pos.size > prev_size + POSITION_THRESHOLD:
                delta = curr_pos.size - prev_size
                logger.info(f"🔔 BUY detected: {curr_pos.market_question[:40]}... (+{delta:.2f} shares)")
                self.process_buy_signal(token_id, curr_pos, delta)
                self.target_positions[token_id] = curr_pos
        
        # Detect SELLs (reduced or closed positions)
        for token_id in list(self.target_positions.keys()):
            prev_pos = self.target_positions[token_id]
            curr_pos = current_positions.get(token_id)
            curr_size = curr_pos.size if curr_pos else Decimal(0)
            
            if curr_size < prev_pos.size - POSITION_THRESHOLD:
                delta = prev_pos.size - curr_size
                logger.info(f"🔔 SELL detected: {prev_pos.market_question[:40]}... (-{delta:.2f} shares)")
                self.process_sell_signal(token_id, prev_pos, delta)
                
                if curr_pos:
                    self.target_positions[token_id] = curr_pos
                else:
                    del self.target_positions[token_id]
        
        # Track any new positions we haven't seen
        for token_id, curr_pos in current_positions.items():
            if token_id not in self.target_positions:
                self.target_positions[token_id] = curr_pos
        
        # Heartbeat with stats
        if time.time() - self.last_heartbeat > 30:
            stats = self.get_performance_summary()
            logger.info(f"💓 Monitoring... {len(self.target_positions)} positions | ${self.cached_my_cash:.2f} cash | {stats}")
            self.last_heartbeat = time.time()

    def get_performance_summary(self) -> str:
        """Get a summary of trading performance."""
        if self.trades_attempted == 0:
            return "No trades yet"
        
        fill_rate = (self.trades_filled / self.trades_attempted) * 100 if self.trades_attempted > 0 else 0
        avg_latency = self.total_latency_ms / self.trades_filled if self.trades_filled > 0 else 0
        avg_slippage = self.total_slippage / self.trades_filled if self.trades_filled > 0 else Decimal(0)
        
        return (f"Trades: {self.trades_filled}/{self.trades_attempted} ({fill_rate:.0f}% fill) | "
                f"Avg latency: {avg_latency:.0f}ms | "
                f"Avg slippage: {avg_slippage:+.4f}")

    def print_trade_history(self):
        """Print detailed trade history."""
        if not self.trade_stats:
            logger.info("No trades recorded yet")
            return
        
        logger.info("\n" + "=" * 70)
        logger.info("📊 TRADE HISTORY")
        logger.info("=" * 70)
        
        for i, t in enumerate(self.trade_stats, 1):
            logger.info(f"{i}. {t.side} {t.market_question}")
            logger.info(f"   Target: {t.target_price:.3f} → Us: {t.our_price:.3f} | "
                       f"Slippage: {t.price_slippage:+.4f} | Latency: {t.latency_ms:.0f}ms")
        
        logger.info("-" * 70)
        logger.info(f"TOTALS: {self.get_performance_summary()}")
        logger.info("=" * 70 + "\n")

    def run(self):
        """Main run loop."""
        logger.info("=" * 60)
        logger.info("⚡ Polymarket Mirror Bot v2 Starting")
        logger.info("=" * 60)
        logger.info(f"Target: {self.target_address}")
        logger.info(f"Poll interval: {POLL_INTERVAL}s")
        logger.info(f"Max capital: ${MAX_CAPITAL_USAGE}")
        logger.info(f"Max buy price: {MAX_BUY_PRICE}")
        logger.info(f"Slippage tolerance: +{MAX_UPPER_SLIPPAGE}/-{MAX_LOWER_SLIPPAGE}")
        logger.info("=" * 60)
        
        try:
            while True:
                try:
                    self.sync_positions()
                except Exception as e:
                    logger.error(f"Loop error: {e}")
                    time.sleep(1)
                
                time.sleep(POLL_INTERVAL)
                
        except KeyboardInterrupt:
            logger.info("\n⏹️ Shutting down...")
            self.print_trade_history()
            logger.info("Goodbye!")


if __name__ == "__main__":
    if not PRIVATE_KEY:
        print("❌ PRIVATE_KEY not set in .env")
        exit(1)
    if not TARGET_ADDRESS:
        print("❌ TARGET_ADDRESS not set in .env")
        exit(1)
    if not FUNDER_ADDRESS:
        print("❌ FUNDER_ADDRESS not set in .env")
        exit(1)
    
    bot = PolymarketMirror(TARGET_ADDRESS, PRIVATE_KEY)
    bot.run()