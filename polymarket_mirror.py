#!/usr/bin/env python3
"""
Polymarket Mirror Trading Bot
Monitors a target account and mirrors their positions in real-time.

Requirements:
    pip install py-clob-client python-dotenv web3 requests websockets eth-account --break-system-packages

Setup:
    1. Create a .env file with your credentials (see .env.example)
    2. Set TARGET_ADDRESS to the wallet you want to mirror
    3. Run: python polymarket_mirror.py

Modes:
    --view <address>     View a wallet's current positions
    --poll               Use polling mode (default, ~30s latency)
    --ws                 Use websocket mode (~1-3s latency)
    --onchain            Use on-chain mode (~0.5s latency) ⚡
    --help               Show detailed help
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

# Websockets for real-time
try:
    import websockets
    from websockets.exceptions import ConnectionClosed as WSConnectionClosed
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    WSConnectionClosed = Exception  # Fallback

# Web3 for on-chain monitoring
try:
    from web3 import Web3
    from eth_account import Account
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

# Polymarket CLOB API
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    # Side constants - try new import path first, fall back to old
    try:
        from py_clob_client.clob_types import BUY, SELL
    except ImportError:
        BUY = "BUY"
        SELL = "SELL"
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    BUY = "BUY"
    SELL = "SELL"
    print("Warning: py-clob-client not installed. Install with: pip install py-clob-client")

load_dotenv()

# Configuration
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "")  # Wallet to mirror
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")        # Your wallet's private key
CHAIN_ID = 137  # Polygon mainnet

# Polymarket Authentication
# FUNDER_ADDRESS = Your Polymarket deposit address (proxy wallet holding funds)
# SIGNATURE_TYPE = 0 for MetaMask/EOA, 1 for Email/Magic wallet
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))  # Default to Magic wallet

# Pre-derived API credentials (optional - will derive from PRIVATE_KEY if not set)
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")

# Manual balance override (if API can't fetch it)
MY_BALANCE = os.getenv("MY_BALANCE", "")  # Your USDC balance, e.g. "126"

# API Endpoints
POLYMARKET_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"  # Was gamma-api, now data-api
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLYGON_WS = os.getenv("POLYGON_WS", "")  # Optional: Alchemy/Infura websocket for on-chain events

# CTF Exchange contract (where trades settle)
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Conditional Tokens (ERC1155) - the actual outcome tokens
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# USDC on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Token decimals for on-chain value conversion
# Polymarket outcome tokens use same precision as USDC collateral
CTF_TOKEN_DECIMALS = 6

# Event signatures (keccak256 hashes)
# TransferSingle(address operator, address from, address to, uint256 id, uint256 value)
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
# TransferBatch(address operator, address from, address to, uint256[] ids, uint256[] values)
TRANSFER_BATCH_TOPIC = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"

# Minimal ABI for decoding events
CONDITIONAL_TOKENS_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "operator", "type": "address"},
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "id", "type": "uint256"},
            {"indexed": False, "name": "value", "type": "uint256"}
        ],
        "name": "TransferSingle",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "operator", "type": "address"},
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "ids", "type": "uint256[]"},
            {"indexed": False, "name": "values", "type": "uint256[]"}
        ],
        "name": "TransferBatch",
        "type": "event"
    }
]

# Mirror settings
POLL_INTERVAL = 30          # seconds between checks (polling mode)
POSITION_THRESHOLD = Decimal("0.01")   # minimum position size to mirror (in shares)
EXECUTION_DELAY = 0.5       # seconds to wait before executing (avoid front-running concerns)

# Position sizing settings
SIZING_MODE = os.getenv("SIZING_MODE", "proportional")  # "proportional", "fixed", or "exact"
# proportional: scale based on your balance vs target's portfolio
# fixed: use fixed percentage of your balance per trade
# exact: mirror exact share counts (dangerous if target is a whale)

MAX_POSITION_PCT = Decimal(os.getenv("MAX_POSITION_PCT", "0.10"))  # max 10% of your balance per position
MAX_POSITION_USDC = Decimal(os.getenv("MAX_POSITION_USDC", "100"))  # absolute max USDC per position
MIN_POSITION_USDC = Decimal(os.getenv("MIN_POSITION_USDC", "0.01"))    # minimum position (avoid dust)
MIRROR_FRACTION = Decimal(os.getenv("MIRROR_FRACTION", "1.0"))      # only for "exact" mode

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
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


@dataclass 
class Trade:
    """Represents a detected trade from the target."""
    timestamp: datetime
    token_id: str
    market_id: str
    side: str  # BUY or SELL
    size: Decimal
    price: Decimal
    tx_hash: str = ""
    market_question: str = ""
    
    def __str__(self):
        question = self.market_question[:40] if self.market_question else "Unknown"
        return f"[{self.timestamp.strftime('%H:%M:%S')}] {self.side} {self.size:.2f} @ {self.price:.3f} - {question}"


class TradeQueue:
    """Thread-safe queue for trades to execute."""
    def __init__(self):
        self.queue: asyncio.Queue[Trade] = asyncio.Queue()
        self.executed: list[Trade] = []
    
    async def add(self, trade: Trade):
        await self.queue.put(trade)
        logger.debug(f"Queued: {trade}")
    
    async def get(self) -> Trade:
        return await self.queue.get()
    
    def mark_executed(self, trade: Trade):
        self.executed.append(trade)


class OnChainMonitor:
    """
    Monitors Polygon blockchain directly for CTF token transfers.
    This provides the lowest latency detection of trades.
    """
    
    def __init__(self, target_address: str, trade_queue: TradeQueue, ws_url: str):
        self.target_address = target_address.lower() if target_address else ""
        self.trade_queue = trade_queue
        self.ws_url = ws_url or ""
        self.w3 = None  # Will be Web3 instance if connected
        self.contract = None
        self.market_cache: dict[str, dict] = {}
        self.pending_txs: set[str] = set()  # Track processed tx hashes
        self.pending_txs_order: list[str] = []  # Maintain insertion order for cleanup
        
    async def connect(self) -> bool:
        """Initialize Web3 connection."""
        if not WEB3_AVAILABLE:
            logger.error("web3 not installed. Run: pip install web3")
            return False
            
        if not self.ws_url:
            logger.error("POLYGON_WS not set in .env")
            return False
        
        try:
            # For websocket subscriptions we need the provider
            # Handle both old and new web3.py versions
            try:
                # web3.py >= 6.0
                from web3.providers import WebSocketProvider
                self.w3 = Web3(WebSocketProvider(self.ws_url))
            except ImportError:
                # web3.py < 6.0 (deprecated but still works)
                self.w3 = Web3(Web3.WebsocketProvider(self.ws_url))
            
            if not self.w3.is_connected():
                logger.error("Failed to connect to Polygon websocket")
                return False
            
            self.contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                abi=CONDITIONAL_TOKENS_ABI
            )
            
            logger.info(f"Connected to Polygon. Block: {self.w3.eth.block_number}")
            return True
        except Exception as e:
            logger.error(f"Web3 connection error: {e}")
            return False

    def _get_market_info(self, token_id: int) -> dict:
        """Look up market info for a token ID."""
        token_id_hex = hex(token_id)
        if token_id_hex in self.market_cache:
            return self.market_cache[token_id_hex]
        
        try:
            url = f"{DATA_API}/markets"
            resp = requests.get(url, params={"clob_token_ids": str(token_id)}, timeout=5)
            if resp.ok:
                data = resp.json()
                if data:
                    market = data[0] if isinstance(data, list) else data
                    self.market_cache[token_id_hex] = market
                    return market
        except Exception as e:
            logger.debug(f"Could not fetch market info for {token_id}: {e}")
        
        return {"question": f"Token {token_id_hex[:16]}...", "outcome": "Unknown"}

    async def _process_transfer(self, event: dict):
        """Process a TransferSingle or TransferBatch event."""
        try:
            # Get transaction hash - handle both bytes and HexBytes
            tx_hash_raw = event.get("transactionHash", "")
            if hasattr(tx_hash_raw, 'hex'):
                tx_hash = tx_hash_raw.hex()
            elif isinstance(tx_hash_raw, bytes):
                tx_hash = tx_hash_raw.hex()
            else:
                tx_hash = str(tx_hash_raw) if tx_hash_raw else ""
            
            # Dedupe
            if tx_hash in self.pending_txs:
                return
            self.pending_txs.add(tx_hash)
            self.pending_txs_order.append(tx_hash)
            
            # Keep set bounded - remove oldest entries
            if len(self.pending_txs) > 1000:
                # Remove oldest 500 entries
                to_remove = self.pending_txs_order[:500]
                self.pending_txs_order = self.pending_txs_order[500:]
                for old_hash in to_remove:
                    self.pending_txs.discard(old_hash)
            
            # Safely get args
            args = event.get("args", {})
            if not args:
                return
            
            # Get addresses - handle both string and bytes
            from_raw = args.get("from", "")
            to_raw = args.get("to", "")
            
            # Convert to lowercase string
            if hasattr(from_raw, 'hex'):
                from_addr = from_raw.hex().lower()
            elif isinstance(from_raw, bytes):
                from_addr = "0x" + from_raw.hex().lower()
            else:
                from_addr = str(from_raw).lower() if from_raw else ""
                
            if hasattr(to_raw, 'hex'):
                to_addr = to_raw.hex().lower()
            elif isinstance(to_raw, bytes):
                to_addr = "0x" + to_raw.hex().lower()
            else:
                to_addr = str(to_raw).lower() if to_raw else ""
            
            # Check if target is involved
            if from_addr != self.target_address and to_addr != self.target_address:
                return
            
            # Determine if this is a buy or sell for target
            if to_addr == self.target_address:
                side = "BUY"
            else:
                side = "SELL"
            
            # Handle single vs batch
            if "id" in args:
                # TransferSingle
                token_ids = [args["id"]]
                values = [args.get("value", 0)]
            elif "ids" in args:
                # TransferBatch
                token_ids = args["ids"]
                values = args.get("values", [])
            else:
                # Unknown format
                return
            
            for token_id, value in zip(token_ids, values):
                # Convert to human-readable size using token decimals
                size = Decimal(str(value)) / Decimal(10 ** CTF_TOKEN_DECIMALS)
                
                if size < POSITION_THRESHOLD:
                    continue
                
                market_info = self._get_market_info(token_id)
                
                trade = Trade(
                    timestamp=datetime.now(),
                    token_id=hex(token_id),
                    market_id=market_info.get("condition_id", ""),
                    side=side,
                    size=size,
                    price=Decimal("0"),  # Will get from orderbook
                    tx_hash=tx_hash,
                    market_question=market_info.get("question", f"Token {hex(token_id)[:16]}")
                )
                
                logger.info(f"⚡ ON-CHAIN: {side} detected - {(trade.market_question or 'Unknown')[:50]}")
                await self.trade_queue.add(trade)
                
        except Exception as e:
            logger.error(f"Error processing transfer event: {e}")

    async def monitor_pending_txs(self):
        """
        Monitor pending transactions in mempool for even faster detection.
        This catches trades BEFORE they're confirmed.
        """
        if not self.w3:
            return
            
        logger.info("Monitoring mempool for pending transactions...")
        
        try:
            # Subscribe to pending transactions
            pending_filter = self.w3.eth.filter("pending")
            
            while True:
                try:
                    new_entries = pending_filter.get_new_entries()
                    for tx_hash in new_entries:
                        await self._check_pending_tx(tx_hash)
                except Exception as e:
                    logger.debug(f"Pending tx check error: {e}")
                
                await asyncio.sleep(0.1)  # 100ms polling
                
        except Exception as e:
            logger.error(f"Mempool monitoring error: {e}")

    async def _check_pending_tx(self, tx_hash):
        """Check if a pending transaction involves our target."""
        try:
            tx = self.w3.eth.get_transaction(tx_hash)
            if not tx:
                return
            
            # Check if it's to the CTF Exchange or Conditional Tokens contract
            to_addr = tx.get("to", "").lower() if tx.get("to") else ""
            from_addr = tx.get("from", "").lower() if tx.get("from") else ""
            
            if to_addr not in [CTF_EXCHANGE.lower(), CONDITIONAL_TOKENS.lower()]:
                return
            
            # Check if our target is involved
            if from_addr == self.target_address:
                # Get hash as string for display
                if hasattr(tx_hash, 'hex'):
                    hash_str = tx_hash.hex()[:16]
                elif isinstance(tx_hash, bytes):
                    hash_str = tx_hash.hex()[:16]
                else:
                    hash_str = str(tx_hash)[:16]
                logger.info(f"🔮 PENDING TX from target detected: {hash_str}...")
                # We can't decode the full trade yet, but we know something is coming
                # The confirmed event will give us details
                
        except Exception:
            pass  # Many pending txs will fail to fetch

    async def monitor_logs(self):
        """Monitor confirmed Transfer events via log subscription."""
        if not self.w3:
            return
        
        if not self.target_address or len(self.target_address) < 40:
            logger.error("Invalid target address for on-chain monitoring")
            return
        
        addr_display = self.target_address[:10] if self.target_address else "unknown"
        logger.info(f"Monitoring CTF transfers for {addr_display}...")
        
        # Create filter for TransferSingle and TransferBatch events
        # Filter for events where target is sender OR receiver
        try:
            # Validate address format
            Web3.to_checksum_address(self.target_address)
        except Exception as e:
            logger.error(f"Invalid target address format: {e}")
            return
            
        # Pad address to 32 bytes (64 hex chars) for topic filtering
        addr_without_prefix = self.target_address[2:] if self.target_address.startswith("0x") else self.target_address
        target_padded = "0x" + addr_without_prefix.lower().zfill(64)
        
        try:
            # We need two filters - one for from, one for to
            # Since indexed params are in topics
            
            # TransferSingle where target is 'from' (topic[2])
            filter_from = self.w3.eth.filter({
                "address": Web3.to_checksum_address(CONDITIONAL_TOKENS),
                "topics": [
                    TRANSFER_SINGLE_TOPIC,
                    None,  # operator (any)
                    target_padded,  # from = target
                    None   # to (any)
                ]
            })
            
            # TransferSingle where target is 'to' (topic[3])
            filter_to = self.w3.eth.filter({
                "address": Web3.to_checksum_address(CONDITIONAL_TOKENS),
                "topics": [
                    TRANSFER_SINGLE_TOPIC,
                    None,  # operator (any)
                    None,  # from (any)
                    target_padded   # to = target
                ]
            })
            
            logger.info("Log filters created. Listening for events...")
            
            while True:
                try:
                    # Check both filters
                    for log in filter_from.get_new_entries():
                        event = self.contract.events.TransferSingle().process_log(log)
                        await self._process_transfer(event)
                    
                    for log in filter_to.get_new_entries():
                        event = self.contract.events.TransferSingle().process_log(log)
                        await self._process_transfer(event)
                        
                except Exception as e:
                    logger.debug(f"Log polling error: {e}")
                
                await asyncio.sleep(0.5)  # 500ms - Polygon block time is ~2s
                
        except Exception as e:
            logger.error(f"Log monitoring error: {e}")
            raise

    async def run(self):
        """Main monitoring loop."""
        if not await self.connect():
            logger.error("Failed to connect. Falling back to CLOB websocket mode.")
            return
        
        # Run log monitoring and optionally mempool monitoring
        tasks = [self.monitor_logs()]
        
        # Mempool monitoring is more resource intensive and not all providers support it
        # Uncomment to enable:
        # tasks.append(self.monitor_pending_txs())
        
        await asyncio.gather(*tasks)


class PolymarketMirror:
    def __init__(self, target_address: str, private_key: str):
        self.target_address = target_address.lower() if target_address else ""
        self.private_key = private_key or ""
        self.my_positions: dict[str, Position] = {}
        self.target_positions: dict[str, Position] = {}
        self.trade_queue = TradeQueue()
        self.market_cache: dict[str, dict] = {}  # token_id -> market info
        self.subscribed_markets: set[str] = set()
        self.last_position_check: float = 0  # Rate limit position checks
        self.position_check_cooldown: float = 2.0  # Minimum seconds between checks
        self.recent_trades: dict[str, float] = {}  # token_id -> timestamp for dedup
        self.trade_dedup_window: float = 5.0  # Ignore duplicate trades within this window
        
        # Portfolio value caching for proportional sizing
        self._target_portfolio_cache: Decimal = Decimal(0)
        self._my_portfolio_cache: Decimal = Decimal(0)
        self._portfolio_cache_time: float = 0
        self._portfolio_cache_ttl: float = 60.0  # Refresh every 60 seconds
        
        # Store funder address (proxy wallet)
        self.funder_address = FUNDER_ADDRESS
        
        if CLOB_AVAILABLE and private_key:
            try:
                from eth_account import Account
                
                # Determine funder address
                # Priority: FUNDER_ADDRESS env var > derive from private key
                if FUNDER_ADDRESS:
                    funder = FUNDER_ADDRESS
                    logger.info(f"Using funder address from config: {funder[:10]}...")
                else:
                    # Derive from private key (works for EOA/MetaMask)
                    funder = Account.from_key(private_key).address
                    logger.info(f"Derived funder address: {funder[:10]}...")
                    logger.warning("No FUNDER_ADDRESS set. If using email/Magic wallet, set it in .env")
                
                self.funder_address = funder.lower()
                
                # Initialize client with signature type
                self.client = ClobClient(
                    POLYMARKET_API,
                    key=private_key,
                    chain_id=CHAIN_ID,
                    signature_type=SIGNATURE_TYPE,
                    funder=funder
                )
                
                # Set up API credentials for L2 auth
                if API_KEY and API_SECRET and API_PASSPHRASE:
                    # Use pre-derived credentials from .env
                    from py_clob_client.clob_types import ApiCreds
                    creds = ApiCreds(
                        api_key=API_KEY,
                        api_secret=API_SECRET,
                        api_passphrase=API_PASSPHRASE
                    )
                    self.client.set_api_creds(creds)
                    logger.info("Using API credentials from config")
                else:
                    # Derive credentials (requires signing with private key)
                    try:
                        creds = self.client.create_or_derive_api_creds()
                        self.client.set_api_creds(creds)
                        logger.info("Derived API credentials from private key")
                    except Exception as e:
                        logger.warning(f"Could not derive API credentials: {e}")
                        logger.warning("Trading may not work. Run: python setup_polymarket.py")
                
                logger.info(f"CLOB client initialized (signature_type={SIGNATURE_TYPE})")
                
            except Exception as e:
                logger.warning(f"Could not initialize CLOB client: {e}")
                self.client = None
        else:
            self.client = None
            if not private_key:
                logger.warning("Running in read-only mode (no PRIVATE_KEY)")
            elif not CLOB_AVAILABLE:
                logger.warning("Running in read-only mode (py-clob-client not installed)")

    def get_account_positions(self, address: str) -> list[Position]:
        """Fetch positions for a given wallet address via Gamma API."""
        positions = []
        if not address:
            return positions
            
        try:
            # Query the Gamma API for user positions
            url = f"{DATA_API}/positions"
            params = {"user": address.lower()}
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            # Handle case where API returns non-list
            if not isinstance(data, list):
                logger.warning(f"Unexpected API response type: {type(data)}")
                return positions
            
            for pos in data:
                if not isinstance(pos, dict):
                    continue
                    
                # Safely convert size and price to Decimal
                try:
                    size_val = pos.get("size", 0)
                    size = Decimal(str(size_val)) if size_val is not None else Decimal(0)
                except (ValueError, TypeError, InvalidOperation):
                    size = Decimal(0)
                    
                try:
                    price_val = pos.get("avgPrice", 0)
                    avg_price = Decimal(str(price_val)) if price_val is not None else Decimal(0)
                except (ValueError, TypeError, InvalidOperation):
                    avg_price = Decimal(0)
                
                if size > POSITION_THRESHOLD:
                    positions.append(Position(
                        market_id=pos.get("marketId", ""),
                        token_id=pos.get("tokenId", ""),
                        outcome=pos.get("outcome", ""),
                        size=size,
                        avg_price=avg_price,
                        market_question=pos.get("question", "Unknown")
                    ))
                    # Cache market info
                    self.market_cache[pos.get("tokenId", "")] = {
                        "market_id": pos.get("marketId", ""),
                        "question": pos.get("question", ""),
                        "outcome": pos.get("outcome", "")
                    }
        except Exception as e:
            logger.error(f"Error fetching positions for {address}: {e}")
        
        return positions

    def get_market_info(self, token_id: str) -> dict:
        """Get market info for a token, fetching if not cached."""
        if token_id in self.market_cache:
            return self.market_cache[token_id]
        
        try:
            url = f"{POLYMARKET_API}/markets"
            resp = requests.get(url, params={"token_id": token_id}, timeout=10)
            if resp.ok:
                data = resp.json()
                if data:
                    self.market_cache[token_id] = data[0] if isinstance(data, list) else data
                    return self.market_cache[token_id]
        except Exception as e:
            logger.debug(f"Could not fetch market info: {e}")
        
        return {"question": "Unknown", "outcome": "Unknown"}

    def get_market_price(self, token_id: str, side: str = "BUY") -> Optional[Decimal]:
        """Get current best price for a token.
        
        Args:
            token_id: The token to get price for
            side: "BUY" returns best ask, "SELL" returns best bid
        """
        if not token_id:
            return None
            
        try:
            url = f"{POLYMARKET_API}/book"
            params = {"token_id": token_id}
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            book = resp.json()
            
            if not isinstance(book, dict):
                return None
            
            if side == "BUY":
                # Get best ask for buying
                asks = book.get("asks", [])
                if asks and isinstance(asks, list) and len(asks) > 0:
                    price = asks[0].get("price") if isinstance(asks[0], dict) else None
                    if price is not None:
                        return Decimal(str(price))
            else:
                # Get best bid for selling
                bids = book.get("bids", [])
                if bids and isinstance(bids, list) and len(bids) > 0:
                    price = bids[0].get("price") if isinstance(bids[0], dict) else None
                    if price is not None:
                        return Decimal(str(price))
            return None
        except Exception as e:
            logger.error(f"Error getting price for {token_id}: {e}")
            return None

    def get_usdc_balance(self, address: str) -> Decimal:
        """Get USDC balance for an address."""
        if not address:
            return Decimal(0)
        
        # Try the /value endpoint (returns equity/cash breakdown)
        try:
            url = f"{DATA_API}/value"
            params = {"user": address.lower()}
            resp = requests.get(url, params=params, timeout=10)
            if resp.ok:
                data = resp.json()
                if isinstance(data, dict):
                    # Response format: {"cash": 126.5, "bets": 100.0, "equity_total": 226.5}
                    cash = data.get("cash", 0)
                    if cash:
                        return Decimal(str(cash))
        except Exception as e:
            logger.debug(f"Could not get balance from /value: {e}")
        
        # Fallback: try /balance endpoint
        try:
            url = f"{DATA_API}/balance"
            params = {"user": address.lower()}
            resp = requests.get(url, params=params, timeout=10)
            if resp.ok:
                data = resp.json()
                if isinstance(data, dict):
                    balance = data.get("balance", data.get("usdc", data.get("available", 0)))
                    if balance is not None:
                        return Decimal(str(balance))
        except Exception:
            pass
        
        # If we can't get balance, return 0
        logger.warning(f"Could not fetch USDC balance for {address[:10]}..., using conservative estimate")
        return Decimal(0)

    def get_portfolio_value(self, address: str) -> Decimal:
        """Calculate total portfolio value (positions + available balance)."""
        if not address:
            return Decimal(0)
        
        # First try /value endpoint which gives total equity
        try:
            url = f"{DATA_API}/value"
            params = {"user": address.lower()}
            resp = requests.get(url, params=params, timeout=10)
            if resp.ok:
                data = resp.json()
                if isinstance(data, dict):
                    equity = data.get("equity_total", data.get("total", 0))
                    if equity:
                        return Decimal(str(equity))
        except Exception as e:
            logger.debug(f"Could not get portfolio from /value: {e}")
        
        # Fallback: calculate from positions
        total_value = Decimal(0)
        
        try:
            positions = self.get_account_positions(address)
            for pos in positions:
                # Value = size * current price (or avg price as fallback)
                if pos.size > 0:
                    current_price = self.get_market_price(pos.token_id, "SELL")
                    if current_price and current_price > 0:
                        total_value += pos.size * current_price
                    else:
                        total_value += pos.size * pos.avg_price
        except Exception as e:
            logger.error(f"Error calculating portfolio value: {e}")
        
        # Add USDC balance
        usdc_balance = self.get_usdc_balance(address)
        total_value += usdc_balance
        
        return total_value

    def get_my_balance(self) -> Decimal:
        """Get our current USDC balance."""
        # Check for manual override first
        if MY_BALANCE:
            try:
                return Decimal(MY_BALANCE)
            except Exception:
                pass
        
        # Try CLOB client with get_balance_allowance (L2 method)
        if self.client:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                
                # Get balance - returns {"balance": "...", "allowances": {...}}
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                result = self.client.get_balance_allowance(params)
                if result and isinstance(result, dict):
                    balance_raw = result.get("balance", "0")
                    if balance_raw:
                        # Balance is in micro-units (6 decimals for USDC)
                        return Decimal(str(balance_raw)) / Decimal("1000000")
            except Exception as e:
                logger.debug(f"Could not get balance from CLOB client: {e}")
        
        # Fallback to API
        my_addr = self._get_my_address()
        if not my_addr:
            return Decimal(0)
        return self.get_usdc_balance(my_addr)

    def get_my_portfolio_value(self) -> Decimal:
        """Get our total portfolio value."""
        my_addr = self._get_my_address()
        if not my_addr:
            return Decimal(0)
        
        # For our own account, we can sum positions + cash
        total = Decimal(0)
        
        # Get positions value
        try:
            positions = self.get_account_positions(my_addr)
            for pos in positions:
                if pos.size > 0:
                    # Use current price if available
                    price = self.get_market_price(pos.token_id, "SELL")
                    if price and price > 0:
                        total += pos.size * price
                    else:
                        total += pos.size * pos.avg_price
        except Exception as e:
            logger.debug(f"Error getting own positions: {e}")
        
        # Add cash balance
        total += self.get_my_balance()
        
        return total

    def get_cached_portfolio_values(self) -> tuple[Decimal, Decimal]:
        """Get cached portfolio values for target and self, refreshing if stale."""
        now = time.time()
        
        if now - self._portfolio_cache_time > self._portfolio_cache_ttl:
            # Refresh cache
            self._target_portfolio_cache = self.get_portfolio_value(self.target_address)
            self._my_portfolio_cache = self.get_my_portfolio_value()
            self._portfolio_cache_time = now
            logger.debug(f"Portfolio cache refreshed: target=${self._target_portfolio_cache:.2f}, ours=${self._my_portfolio_cache:.2f}")
        
        return self._target_portfolio_cache, self._my_portfolio_cache

    def calculate_mirror_size(self, target_size: Decimal, current_price: Decimal, 
                              target_portfolio: Decimal = None) -> Decimal:
        """
        Calculate position size based on sizing mode.
        
        Modes:
        - proportional: Scale based on (your portfolio / target portfolio)
        - fixed: Use fixed % of your balance per trade
        - exact: Mirror exact share count (dangerous!)
        
        Args:
            target_size: Number of shares the target is holding
            current_price: Current market price
            target_portfolio: Target's total portfolio value (for proportional mode)
        """
        if current_price <= 0:
            logger.warning("Price is zero or negative, cannot calculate size")
            return Decimal(0)
        
        if target_size <= 0:
            return Decimal(0)
        
        # Get our available balance
        my_balance = self.get_my_balance()
        my_portfolio = self.get_my_portfolio_value()
        
        # Use the larger of balance or portfolio for sizing
        my_capital = max(my_balance, my_portfolio) if my_portfolio > 0 else my_balance
        
        if my_capital <= 0:
            logger.warning("No capital available for trading")
            return Decimal(0)
        
        # Calculate position value target is holding
        target_position_value = target_size * current_price
        
        if SIZING_MODE == "exact":
            # Just mirror exact size (scaled by MIRROR_FRACTION)
            desired_size = target_size * MIRROR_FRACTION
            
        elif SIZING_MODE == "fixed":
            # Use fixed percentage of our capital
            max_for_position = my_capital * MAX_POSITION_PCT
            desired_value = min(target_position_value, max_for_position)
            desired_size = desired_value / current_price
            
        else:  # proportional (default)
            # Scale based on relative portfolio sizes
            if target_portfolio and target_portfolio > 0:
                # Proportional: if target has $100k and we have $1k, we buy 1% of their size
                ratio = my_capital / target_portfolio
                desired_size = target_size * ratio
                logger.debug(f"Proportional sizing: ratio={ratio:.4f}, target_size={target_size}, our_size={desired_size}")
            else:
                # Fallback: use target position as % of their portfolio estimate
                # Assume target's position is X% of their portfolio, match that %
                # Conservative estimate: assume this position is 10% of target's portfolio
                estimated_target_portfolio = target_position_value * 10
                ratio = my_capital / estimated_target_portfolio
                desired_size = target_size * ratio
                logger.debug(f"Proportional sizing (estimated): ratio={ratio:.4f}")
        
        # Apply caps
        position_value = desired_size * current_price
        
        # Cap by percentage of our capital
        max_by_pct = my_capital * MAX_POSITION_PCT
        if position_value > max_by_pct:
            desired_size = max_by_pct / current_price
            logger.debug(f"Capped by MAX_POSITION_PCT ({MAX_POSITION_PCT*100}%): {desired_size}")
        
        # Cap by absolute max
        if position_value > MAX_POSITION_USDC:
            desired_size = MAX_POSITION_USDC / current_price
            logger.debug(f"Capped by MAX_POSITION_USDC (${MAX_POSITION_USDC}): {desired_size}")
        
        # Minimum position check
        final_value = desired_size * current_price
        if final_value < MIN_POSITION_USDC:
            logger.debug(f"Position too small (${final_value} < ${MIN_POSITION_USDC}), skipping")
            return Decimal(0)
        
        return desired_size

    def place_order(self, token_id: str, side: str, size: Decimal, price: Decimal) -> bool:
        """Place an order on Polymarket."""
        if not self.client:
            logger.info(f"[DRY RUN] Would {side} {size} shares at {price}")
            return False
        
        try:
            # Validate market exists and is tradeable
            try:
                book = self.client.get_order_book(token_id)
                if not book or (not book.bids and not book.asks):
                    logger.warning(f"No liquidity for token {token_id[:20]}..., skipping")
                    return False
            except Exception as e:
                logger.warning(f"Market not found or closed for token {token_id[:20]}...: {e}")
                return False
            
            # Convert side string to proper constant
            order_side = BUY if side.upper() == "BUY" else SELL
            
            # Use FOK (Fill-or-Kill) for immediate execution, falls back to GTC
            # FOK ensures we either get filled immediately or cancel
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=order_side,
                order_type=OrderType.GTC  # GTC is safer for limit orders
            )
            result = self.client.create_and_post_order(order_args)
            
            if result and hasattr(result, 'success') and not result.success:
                logger.error(f"Order rejected: {result.errorMsg if hasattr(result, 'errorMsg') else result}")
                return False
                
            logger.info(f"Order placed: {result}")
            return True
        except Exception as e:
            error_str = str(e).lower()
            if "not found" in error_str or "invalid" in error_str:
                logger.warning(f"Market/token not found: {token_id[:20]}...")
            elif "closed" in error_str or "resolved" in error_str:
                logger.warning(f"Market closed/resolved: {token_id[:20]}...")
            elif "balance" in error_str or "allowance" in error_str:
                logger.error(f"Insufficient balance/allowance: {e}")
            else:
                logger.error(f"Error placing order: {e}")
            return False

    def sync_positions(self):
        """Compare target positions to ours and execute trades to mirror."""
        addr_display = self.target_address[:10] if self.target_address else "unknown"
        logger.info(f"Syncing positions with {addr_display}...")
        
        target_positions = self.get_account_positions(self.target_address)
        target_by_token = {p.token_id: p for p in target_positions}
        
        logger.info(f"Target has {len(target_positions)} positions")
        
        # Get target's total portfolio value for proportional sizing
        target_portfolio = self.get_portfolio_value(self.target_address)
        my_portfolio = self.get_my_portfolio_value()
        
        if SIZING_MODE == "proportional":
            logger.info(f"Portfolio comparison: Target=${target_portfolio:.2f}, Ours=${my_portfolio:.2f}")
            if target_portfolio > 0:
                ratio = my_portfolio / target_portfolio
                logger.info(f"Sizing ratio: {ratio:.4f} (we'll size positions at {ratio*100:.2f}% of target)")
                min_target_value = MIN_POSITION_USDC / ratio if ratio > 0 else Decimal("inf")
                logger.info(f"Minimum target position to mirror: ${min_target_value:.2f}")
        
        # Get our current positions
        if self.private_key:
            my_positions = self.get_account_positions(self._get_my_address())
            my_by_token = {p.token_id: p for p in my_positions}
            logger.info(f"We have {len(my_positions)} positions")
        else:
            my_by_token = {}
        
        # Track stats
        checked = 0
        skipped_too_small = 0
        skipped_already_have = 0
        to_buy = 0
        
        # Find new positions to enter
        for token_id, target_pos in target_by_token.items():
            checked += 1
            my_pos = my_by_token.get(token_id)
            my_size = my_pos.size if my_pos else Decimal(0)
            
            if target_pos.size > my_size:
                # Target has a larger position, we need to buy
                price = self.get_market_price(token_id)
                if price:
                    size_to_buy = self.calculate_mirror_size(
                        target_pos.size - my_size, 
                        price,
                        target_portfolio=target_portfolio
                    )
                    if size_to_buy > POSITION_THRESHOLD:
                        to_buy += 1
                        question = target_pos.market_question or "Unknown"
                        outcome = target_pos.outcome or "?"
                        position_value = size_to_buy * price
                        logger.info(
                            f"MIRROR: Buying {size_to_buy:.2f} shares (${position_value:.2f}) of "
                            f"'{question[:50]}' ({outcome}) "
                            f"@ {price:.3f}"
                        )
                        self.place_order(token_id, "BUY", size_to_buy, price)
                    else:
                        skipped_too_small += 1
            else:
                skipped_already_have += 1
        
        logger.info(f"Sync summary: {checked} checked, {to_buy} bought, {skipped_too_small} too small, {skipped_already_have} already matched")
        
        # Find positions to exit (target closed but we still have)
        for token_id, my_pos in my_by_token.items():
            if token_id not in target_by_token:
                question = my_pos.market_question or "Unknown"
                logger.info(
                    f"MIRROR: Target exited '{question}' - consider selling"
                )
                # Could auto-sell here, but leaving as manual for safety

    def _get_my_address(self) -> str:
        """Get the address where our funds are (funder/proxy wallet)."""
        # If funder address is set, use it (this is where funds actually are)
        if self.funder_address:
            return self.funder_address.lower()
        
        # Fallback: derive from private key (for EOA/MetaMask users without proxy)
        if not self.private_key:
            return ""
        try:
            from eth_account import Account
            return Account.from_key(self.private_key).address.lower()
        except ImportError:
            logger.warning("eth_account not installed, cannot derive address")
            return ""
        except Exception as e:
            logger.warning(f"Could not derive address from private key: {e}")
            return ""

    # =========================================================================
    # WEBSOCKET MODE - Real-time trade monitoring
    # =========================================================================
    
    async def monitor_trades_ws(self):
        """Monitor target's trades via Polymarket websocket."""
        if not WS_AVAILABLE:
            logger.error("websockets not installed. Run: pip install websockets")
            return
        
        # First, get target's current positions to know which markets to watch
        positions = self.get_account_positions(self.target_address)
        # Filter out empty/None market_ids
        market_ids = list(set(p.market_id for p in positions if p.market_id))
        
        if not market_ids:
            logger.warning("Target has no positions. Will rely on polling to detect new markets.")
            # Don't return - keep the coroutine running but skip websocket subscription
            # The _poll_for_new_markets task will detect new positions
            while True:
                await asyncio.sleep(60)  # Keep coroutine alive
                # Re-check for positions periodically
                positions = self.get_account_positions(self.target_address)
                market_ids = list(set(p.market_id for p in positions if p.market_id))
                if market_ids:
                    logger.info(f"Target now has positions. Subscribing to {len(market_ids)} markets...")
                    break
        
        logger.info(f"Subscribing to {len(market_ids)} markets...")
        
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10, close_timeout=5) as ws:
                    # Subscribe to markets
                    for market_id in market_ids:
                        sub_msg = {
                            "type": "subscribe",
                            "channel": "market",
                            "market": market_id
                        }
                        await ws.send(json.dumps(sub_msg))
                        self.subscribed_markets.add(market_id)
                    
                    logger.info("WebSocket connected. Listening for trades...")
                    
                    async for message in ws:
                        await self._handle_ws_message(message)
                        
            except WSConnectionClosed:
                logger.warning("WebSocket disconnected. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)

    async def _poll_for_new_markets(self):
        """Background task to detect when target enters new markets."""
        known_markets = set(m for m in self.subscribed_markets if m)  # Filter out empty
        
        while True:
            await asyncio.sleep(60)  # Check every minute
            try:
                positions = self.get_account_positions(self.target_address)
                # Filter out empty/None market_ids
                current_markets = set(p.market_id for p in positions if p.market_id)
                new_markets = current_markets - known_markets
                
                # Remove empty string if present
                new_markets.discard("")
                new_markets.discard(None)
                
                if new_markets:
                    logger.info(f"🆕 Target entered {len(new_markets)} new market(s):")
                    for market_id in list(new_markets)[:5]:  # Show first 5
                        # Try to get market name
                        pos = next((p for p in positions if p.market_id == market_id), None)
                        if pos and pos.market_question:
                            logger.info(f"   → {pos.market_question[:60]}")
                    if len(new_markets) > 5:
                        logger.info(f"   → ... and {len(new_markets) - 5} more")
                    for market_id in new_markets:
                        # Find the position for this market
                        for pos in positions:
                            if pos.market_id == market_id:
                                trade = Trade(
                                    timestamp=datetime.now(),
                                    token_id=pos.token_id,
                                    market_id=market_id,
                                    side="BUY",
                                    size=pos.size,
                                    price=pos.avg_price,
                                    market_question=pos.market_question
                                )
                                await self.trade_queue.add(trade)
                                break
                    known_markets.update(new_markets)
                    
            except Exception as e:
                logger.error(f"Error polling for new markets: {e}")

    async def _handle_ws_message(self, message: str):
        """Process websocket message and detect target's trades."""
        try:
            data = json.loads(message)
            
            # Handle different message types
            msg_type = data.get("type", data.get("event_type", ""))
            
            if msg_type in ["trade", "last_trade_price"]:
                # This is a trade event
                await self._process_trade_event(data)
            elif msg_type == "price_change":
                # Could use this for better execution timing
                pass
                
        except json.JSONDecodeError:
            logger.debug(f"Non-JSON message: {str(message)[:100] if message else 'empty'}")
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def _process_trade_event(self, data: dict):
        """Check if trade involves target address and queue for mirroring."""
        # The websocket doesn't directly tell us WHO traded, so we need to
        # either: (1) query on-chain, (2) poll positions after trade, or
        # (3) use Polygon websocket for Transfer events
        
        # Strategy: When we see a trade in a subscribed market, quickly
        # check if target's position changed
        token_id = data.get("asset_id", data.get("token_id", ""))
        
        if not token_id:
            return
        
        # Rate limit position checks to avoid hammering the API
        now = time.time()
        if now - self.last_position_check < self.position_check_cooldown:
            return
        self.last_position_check = now
        
        # Quick position check
        old_positions = {tid: pos for tid, pos in self.target_positions.items()}
        new_positions_list = self.get_account_positions(self.target_address)
        new_positions = {p.token_id: p for p in new_positions_list}
        
        # Check for increased or new positions
        for pos in new_positions_list:
            old_pos = old_positions.get(pos.token_id)
            old_size = old_pos.size if old_pos else Decimal(0)
            
            if pos.size > old_size:
                # Check dedup window
                last_trade_time = self.recent_trades.get(f"BUY:{pos.token_id}", 0)
                if now - last_trade_time < self.trade_dedup_window:
                    continue
                self.recent_trades[f"BUY:{pos.token_id}"] = now
                
                # Target increased position
                delta = pos.size - old_size
                question = (pos.market_question or 'Unknown')[:50]
                logger.info(f"🎯 TARGET BOUGHT: +{delta:.2f} shares of '{question}'")
                
                trade = Trade(
                    timestamp=datetime.now(),
                    token_id=pos.token_id,
                    market_id=pos.market_id,
                    side="BUY",
                    size=delta,
                    price=pos.avg_price,  # Approximate
                    market_question=pos.market_question
                )
                await self.trade_queue.add(trade)
            elif pos.size < old_size and old_size > 0:
                # Check dedup window
                last_trade_time = self.recent_trades.get(f"SELL:{pos.token_id}", 0)
                if now - last_trade_time < self.trade_dedup_window:
                    continue
                self.recent_trades[f"SELL:{pos.token_id}"] = now
                
                # Target decreased position
                delta = old_size - pos.size
                question = (pos.market_question or 'Unknown')[:50]
                logger.info(f"🎯 TARGET SOLD: -{delta:.2f} shares of '{question}'")
                
                trade = Trade(
                    timestamp=datetime.now(),
                    token_id=pos.token_id,
                    market_id=pos.market_id,
                    side="SELL",
                    size=delta,
                    price=pos.avg_price,
                    market_question=pos.market_question
                )
                await self.trade_queue.add(trade)
        
        # Check for fully closed positions (was in old, not in new)
        for closed_token_id, closed_pos in old_positions.items():
            if closed_token_id not in new_positions:
                # Check dedup window
                last_trade_time = self.recent_trades.get(f"SELL:{closed_token_id}", 0)
                if now - last_trade_time < self.trade_dedup_window:
                    continue
                self.recent_trades[f"SELL:{closed_token_id}"] = now
                
                # Position fully closed
                question = (closed_pos.market_question or 'Unknown')[:50]
                logger.info(f"🎯 TARGET EXITED: Closed entire position in '{question}'")
                
                trade = Trade(
                    timestamp=datetime.now(),
                    token_id=closed_token_id,
                    market_id=closed_pos.market_id,
                    side="SELL",
                    size=closed_pos.size,
                    price=Decimal(0),  # Will get from orderbook
                    market_question=closed_pos.market_question
                )
                await self.trade_queue.add(trade)
        
        # Update cached positions
        self.target_positions = new_positions
        
        # Cleanup old dedup entries to prevent memory leak
        cutoff = now - (self.trade_dedup_window * 10)
        self.recent_trades = {k: v for k, v in self.recent_trades.items() if v > cutoff}

    async def execute_trades(self):
        """Worker that executes queued trades."""
        logger.info("Trade executor started")
        logger.info(f"Sizing mode: {SIZING_MODE}")
        
        while True:
            try:
                trade = await self.trade_queue.get()
                
                # Small delay to avoid appearing to front-run
                await asyncio.sleep(EXECUTION_DELAY)
                
                # Get current market price (ask for buys, bid for sells)
                price = self.get_market_price(trade.token_id, trade.side)
                if not price:
                    logger.warning(f"Could not get {trade.side} price for {trade.token_id}, skipping")
                    continue
                
                # Get cached portfolio values for proportional sizing
                target_portfolio, my_portfolio = self.get_cached_portfolio_values()
                
                # Calculate our size
                question = (trade.market_question or "Unknown")[:40]
                if trade.side == "BUY":
                    size = self.calculate_mirror_size(trade.size, price, target_portfolio=target_portfolio)
                    if size > POSITION_THRESHOLD:
                        position_value = size * price
                        logger.info(f"✅ COPIED: BUY {size:.2f} shares (${position_value:.2f}) @ {price:.3f} - {question}")
                        self.place_order(trade.token_id, "BUY", size, price)
                    else:
                        logger.debug(f"Skipping small position: {size:.4f} shares")
                else:
                    # For sells, check our current position and scale proportionally
                    my_positions = self.get_account_positions(self._get_my_address())
                    my_pos = next((p for p in my_positions if p.token_id == trade.token_id), None)
                    if my_pos and my_pos.size > POSITION_THRESHOLD:
                        # Calculate what fraction of position target is selling
                        if SIZING_MODE == "proportional" and target_portfolio > 0:
                            # Sell same proportion as target
                            target_positions = self.get_account_positions(self.target_address)
                            target_pos = next((p for p in target_positions if p.token_id == trade.token_id), None)
                            if target_pos and target_pos.size > 0:
                                # Target sold trade.size out of target_pos.size + trade.size (what they had)
                                sell_fraction = trade.size / (target_pos.size + trade.size)
                                sell_size = my_pos.size * sell_fraction
                            else:
                                # Target sold entire position
                                sell_size = my_pos.size
                        else:
                            sell_size = min(trade.size * MIRROR_FRACTION, my_pos.size)
                        
                        if sell_size > POSITION_THRESHOLD:
                            position_value = sell_size * price
                            logger.info(f"✅ COPIED: SELL {sell_size:.2f} shares (${position_value:.2f}) @ {price:.3f} - {question}")
                            self.place_order(trade.token_id, "SELL", sell_size, price)
                
                self.trade_queue.mark_executed(trade)
                
            except Exception as e:
                logger.error(f"Error executing trade: {e}")
                await asyncio.sleep(1)

    async def run_websocket_mode(self):
        """Run in websocket mode for real-time mirroring."""
        logger.info("=" * 60)
        logger.info("Polymarket Mirror Bot - WEBSOCKET MODE")
        logger.info(f"Target: {self.target_address}")
        logger.info(f"Sizing mode: {SIZING_MODE}")
        if SIZING_MODE == "proportional":
            logger.info(f"  Max position: {MAX_POSITION_PCT * 100}% of portfolio (cap: ${MAX_POSITION_USDC})")
        elif SIZING_MODE == "fixed":
            logger.info(f"  Fixed: {MAX_POSITION_PCT * 100}% of balance per trade (cap: ${MAX_POSITION_USDC})")
        else:
            logger.info(f"  Exact mirror: {MIRROR_FRACTION * 100}% of target size (cap: ${MAX_POSITION_USDC})")
        logger.info("=" * 60)
        
        if not self.target_address:
            logger.error("No TARGET_ADDRESS set!")
            return
        
        # Log initial portfolio comparison
        target_portfolio = self.get_portfolio_value(self.target_address)
        my_portfolio = self.get_my_portfolio_value()
        logger.info(f"Portfolio values: Target=${target_portfolio:.2f}, Ours=${my_portfolio:.2f}")
        if target_portfolio > 0 and my_portfolio > 0:
            ratio = my_portfolio / target_portfolio
            logger.info(f"Sizing ratio: {ratio:.4f} (positions will be {ratio*100:.2f}% of target size)")
        
        # Load initial positions
        positions = self.get_account_positions(self.target_address)
        self.target_positions = {p.token_id: p for p in positions}
        logger.info(f"Target has {len(positions)} existing positions")
        
        # Run monitor, executor, and market discovery concurrently
        await asyncio.gather(
            self.monitor_trades_ws(),
            self.execute_trades(),
            self._poll_for_new_markets()  # Discovers new markets target enters
        )

    async def run_onchain_mode(self):
        """
        Run in on-chain mode for lowest latency.
        Monitors Polygon blockchain directly for CTF token transfers.
        """
        logger.info("=" * 60)
        logger.info("Polymarket Mirror Bot - ON-CHAIN MODE ⚡")
        logger.info(f"Target: {self.target_address}")
        logger.info(f"Sizing mode: {SIZING_MODE}")
        if SIZING_MODE == "proportional":
            logger.info(f"  Max position: {MAX_POSITION_PCT * 100}% of portfolio (cap: ${MAX_POSITION_USDC})")
        elif SIZING_MODE == "fixed":
            logger.info(f"  Fixed: {MAX_POSITION_PCT * 100}% of balance per trade (cap: ${MAX_POSITION_USDC})")
        else:
            logger.info(f"  Exact mirror: {MIRROR_FRACTION * 100}% of target size (cap: ${MAX_POSITION_USDC})")
        logger.info("=" * 60)
        
        if not self.target_address:
            logger.error("No TARGET_ADDRESS set!")
            return
        
        if not POLYGON_WS:
            logger.error("POLYGON_WS not set! Get a websocket URL from Alchemy or Infura.")
            logger.error("Example: wss://polygon-mainnet.g.alchemy.com/v2/YOUR_API_KEY")
            return
        
        if not WEB3_AVAILABLE:
            logger.error("web3 not installed. Run: pip install web3 --break-system-packages")
            return
        
        # Log initial portfolio comparison
        target_portfolio = self.get_portfolio_value(self.target_address)
        my_portfolio = self.get_my_portfolio_value()
        logger.info(f"Portfolio values: Target=${target_portfolio:.2f}, Ours=${my_portfolio:.2f}")
        if target_portfolio > 0 and my_portfolio > 0:
            ratio = my_portfolio / target_portfolio
            logger.info(f"Sizing ratio: {ratio:.4f} (positions will be {ratio*100:.2f}% of target size)")
        
        # Load initial positions for reference
        positions = self.get_account_positions(self.target_address)
        self.target_positions = {p.token_id: p for p in positions}
        logger.info(f"Target has {len(positions)} existing positions")
        
        # Create on-chain monitor
        onchain_monitor = OnChainMonitor(
            self.target_address, 
            self.trade_queue,
            POLYGON_WS
        )
        
        # Share market cache
        onchain_monitor.market_cache = self.market_cache
        
        # Run on-chain monitor and trade executor concurrently
        await asyncio.gather(
            onchain_monitor.run(),
            self.execute_trades(),
            self._poll_for_new_markets()  # Backup for detecting new market entries
        )

    def run(self):
        """Main loop."""
        logger.info("=" * 60)
        logger.info("Polymarket Mirror Bot - POLLING MODE")
        logger.info(f"Target: {self.target_address}")
        logger.info(f"Poll interval: {POLL_INTERVAL}s")
        logger.info(f"Sizing mode: {SIZING_MODE}")
        if SIZING_MODE == "proportional":
            logger.info(f"  Max position: {MAX_POSITION_PCT * 100}% of portfolio (cap: ${MAX_POSITION_USDC})")
        elif SIZING_MODE == "fixed":
            logger.info(f"  Fixed: {MAX_POSITION_PCT * 100}% of balance per trade (cap: ${MAX_POSITION_USDC})")
        else:
            logger.info(f"  Exact mirror: {MIRROR_FRACTION * 100}% of target size (cap: ${MAX_POSITION_USDC})")
        logger.info("=" * 60)
        
        if not self.target_address:
            logger.error("No TARGET_ADDRESS set. Check your .env file.")
            return
        
        # Log initial portfolio comparison
        target_portfolio = self.get_portfolio_value(self.target_address)
        my_portfolio = self.get_my_portfolio_value()
        logger.info(f"Portfolio values: Target=${target_portfolio:.2f}, Ours=${my_portfolio:.2f}")
        if target_portfolio > 0 and my_portfolio > 0:
            ratio = my_portfolio / target_portfolio
            logger.info(f"Sizing ratio: {ratio:.4f} (positions will be {ratio*100:.2f}% of target size)")
        
        while True:
            try:
                self.sync_positions()
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
            
            logger.info(f"Sleeping {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)


def print_target_positions(address: str):
    """Utility to just view a target's positions."""
    if not address or len(address) < 10:
        print("Invalid address provided")
        return
        
    mirror = PolymarketMirror(address, "")
    positions = mirror.get_account_positions(address)
    
    # Safe display of address
    addr_display = f"{address[:10]}...{address[-6:]}" if len(address) > 16 else address
    print(f"\nPositions for {addr_display}:")
    print("-" * 80)
    
    if not positions:
        print("No positions found.")
        return
    
    for pos in sorted(positions, key=lambda x: x.size or Decimal(0), reverse=True):
        outcome = (pos.outcome or "?")[:5]
        question = (pos.market_question or "Unknown")[:60]
        print(f"  {outcome:5} | {pos.size:>10.2f} shares @ {pos.avg_price:.3f}")
        print(f"        {question}")
        print()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--view":
        # Just view target positions
        addr = sys.argv[2] if len(sys.argv) > 2 else TARGET_ADDRESS
        if addr:
            print_target_positions(addr)
        else:
            print("Usage: python polymarket_mirror.py --view <address>")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "--onchain":
        # On-chain mode for lowest latency
        if not WEB3_AVAILABLE:
            print("Error: web3 not installed")
            print("Run: pip install web3 --break-system-packages")
            sys.exit(1)
        
        if not POLYGON_WS:
            print("Error: POLYGON_WS not set in .env")
            print("Get a websocket URL from:")
            print("  - Alchemy: https://alchemy.com (free tier available)")
            print("  - Infura: https://infura.io")
            print("Example: wss://polygon-mainnet.g.alchemy.com/v2/YOUR_API_KEY")
            sys.exit(1)
        
        bot = PolymarketMirror(TARGET_ADDRESS, PRIVATE_KEY)
        asyncio.run(bot.run_onchain_mode())
    
    elif len(sys.argv) > 1 and sys.argv[1] == "--ws":
        # Websocket mode for real-time mirroring
        if not WS_AVAILABLE:
            print("Error: websockets not installed")
            print("Run: pip install websockets --break-system-packages")
            sys.exit(1)
        
        bot = PolymarketMirror(TARGET_ADDRESS, PRIVATE_KEY)
        asyncio.run(bot.run_websocket_mode())
    
    elif len(sys.argv) > 1 and sys.argv[1] in ["--help", "-h"]:
        print("""
Polymarket Mirror Trading Bot
=============================

Monitors a target wallet and mirrors their prediction market positions.

MODES:
  (default)      Polling mode - checks positions every 30 seconds
  --ws           WebSocket mode - monitors CLOB for market activity (~1-3s latency)
  --onchain      On-chain mode - monitors Polygon directly (~0.5s latency) ⚡
  --view ADDR    View a wallet's current positions (read-only)

SETUP:
  1. Copy env.example to .env
  2. Set TARGET_ADDRESS to the wallet you want to copy
  3. Set PRIVATE_KEY for live trading (omit for dry-run)
  4. For --onchain mode, set POLYGON_WS (get from Alchemy/Infura)

EXAMPLES:
  python polymarket_mirror.py --view 0x1234...   # View positions
  python polymarket_mirror.py                     # Start polling
  python polymarket_mirror.py --ws                # Real-time via CLOB
  python polymarket_mirror.py --onchain           # Lowest latency

LATENCY COMPARISON:
  Polling:   ~30 seconds (configurable)
  WebSocket: ~1-3 seconds  
  On-chain:  ~0.5 seconds (requires POLYGON_WS)
""")
        sys.exit(0)
    
    else:
        # Default: polling mode
        if "--poll" not in sys.argv and len(sys.argv) > 1:
            print("Unknown option. Use --help for usage.")
            sys.exit(1)
        
        bot = PolymarketMirror(TARGET_ADDRESS, PRIVATE_KEY)
        bot.run()