#!/usr/bin/env python3
"""
Polymarket Mirror Bot (V8.1 - Web3 v7 Fixed)
--------------------------------------------
✅ FIX: Updated import to 'WebSocketProvider' (Required for Web3 v7+)
✅ FIX: Robust Payload Parsing
✅ STATUS: Verbose Logging Enabled
"""

import sys
import io

# Force UTF-8 encoding for all output streams on Windows
if sys.platform.startswith('win'):
    # Set environment variable for subprocesses
    import os
    os.environ["PYTHONIOENCODING"] = "utf-8"
    
    # Redirect stdout/stderr to use UTF-8
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
from typing import Optional
from dotenv import load_dotenv
import requests

# --- Dependency Checks ---
try:
    # 🚨 V7 FIX: Import WebSocketProvider (Renamed from WebsocketProviderV2)
    from web3 import AsyncWeb3, AsyncHTTPProvider, WebSocketProvider
    WEB3_AVAILABLE = True
except ImportError:
    print("CRITICAL: pip install web3>=7.0.0 websockets")
    exit(1)

# --- Dependency Checks ---
try:
    # 🚨 V7 FIX: Import WebSocketProvider (Renamed from WebsocketProviderV2)
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
    print("TROUBLESHOOTING TIPS:")
    print("1. Check your Python version (requires 3.9+)")
    print("2. Try: pip install --force-reinstall --no-cache-dir py-clob-client")
    print("3. Ensure you're in the correct virtual environment")
    exit(1)

load_dotenv()

# --- Logging Setup ---
log_file = "bot_v8.log"
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
logger = logging.getLogger("PolyV8.1")

# --- Strict Configuration ---
def get_env_strict(key, default=None):
    val = os.getenv(key, default)
    if not val:
        logger.critical(f"❌ STARTUP FAILED: Missing Env Var: {key}")
        raise ValueError(f"Missing required env var: {key}")
    return val.strip()

PRIVATE_KEY = get_env_strict("PRIVATE_KEY")
FUNDER_ADDRESS = get_env_strict("FUNDER_ADDRESS")

# Validate WSS
POLYGON_WSS = get_env_strict("POLYGON_WS")
if not POLYGON_WSS.startswith("wss://"):
    logger.critical(f"❌ CONFIG ERROR: POLYGON_WS must start with 'wss://'. Got: {POLYGON_WSS[:10]}...")
    raise ValueError("Invalid WSS URL Scheme")

POLYGON_HTTP = get_env_strict("POLYGON_HTTP", "https://polygon-rpc.com")
CHAIN_ID = 137

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYMARKET_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# --- ABIs ---
CTF_ABI = [
    {"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"operator","type":"address"},{"indexed":True,"internalType":"address","name":"from","type":"address"},{"indexed":True,"internalType":"address","name":"to","type":"address"},{"indexed":False,"internalType":"uint256","name":"id","type":"uint256"},{"indexed":False,"internalType":"uint256","name":"value","type":"uint256"}],"name":"TransferSingle","type":"event"},
    {"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"operator","type":"address"},{"indexed":True,"internalType":"address","name":"from","type":"address"},{"indexed":True,"internalType":"address","name":"to","type":"address"},{"indexed":False,"internalType":"uint256[]","name":"ids","type":"uint256[]"},{"indexed":False,"internalType":"uint256[]","name":"values","type":"uint256[]"}],"name":"TransferBatch","type":"event"},
    {"inputs":[{"internalType":"address","name":"collateralToken","type":"address"},{"internalType":"bytes32","name":"parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}
]
ERC20_ABI = [
    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"payable":False,"stateMutability":"view","type":"function"}
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

# --- Managers ---
class ConfigManager:
    def __init__(self, filepath="whitelist.json"):
        self.filepath = filepath
        self.traders = {}
        self.addresses = set()
        self.last_load = 0
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
        except Exception as e:
            logger.error(f"❌ Config Load Error: {e}")

    def get(self, addr, key, default=None):
        return self.traders.get(addr.lower(), {}).get(key, self.global_settings.get(key, default))

class AssetManager:
    def __init__(self, w3: AsyncWeb3):
        self.w3 = w3
        self.market_cache = {}
        self.token_decimals = {
            "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": 6,  # USDC
            "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619": 18, # WETH
        }

    async def get_decimals(self, token_addr: str) -> Optional[int]:
        token_addr = token_addr.lower()
        if token_addr in self.token_decimals:
            return self.token_decimals[token_addr]
        
        try:
            contract = self.w3.eth.contract(address=self.w3.to_checksum_address(token_addr), abi=ERC20_ABI)
            decimals = await contract.functions.decimals().call()
            self.token_decimals[token_addr] = decimals
            logger.info(f"🆕 Decimals Found: {token_addr[:8]}... = {decimals}")
            return decimals
        except Exception as e:
            logger.error(f"❌ FAILED to fetch decimals for {token_addr}: {e}")
            return None 

    async def get_market_info(self, token_id: int):
        tid_str = str(token_id)
        if tid_str in self.market_cache: return self.market_cache[tid_str]

        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(None, lambda: requests.get(
                f"{DATA_API}/markets", params={"clob_token_ids": tid_str}, timeout=3
            ))
            if resp.ok and (data := resp.json()):
                m = data[0]
                info = {
                    "question": m.get("question", "Unknown"),
                    "condition_id": m.get("condition_id"),
                    "collateral": m.get("collateral_token", "").lower(),
                    "active": m.get("active", True),
                    "closed": m.get("closed", False)
                }
                self.market_cache[tid_str] = info
                return info
        except Exception as e:
            logger.warning(f"⚠️ Market Data API Error: {e}")
        return None

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

# --- Auto Redeemer ---
class AutoRedeemer:
    def __init__(self, w3: AsyncWeb3, client: ClobClient, asset_mgr: AssetManager):
        self.w3 = w3
        self.client = client
        self.asset_mgr = asset_mgr
        self.signer = SecureSigner(w3)
        self.ctf_contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
        self.running = False

    async def redeem_cycle(self):
        try:
            logger.info("♻️  Scanning for redeemable winnings...")
            loop = asyncio.get_running_loop()
            positions = await loop.run_in_executor(None, lambda: self.client.get_positions(limit=100))
            
            redeemable = []
            for p in positions:
                if Decimal(p['size']) <= 0: continue
                info = await self.asset_mgr.get_market_info(p['asset_id'])
                if info and (info['closed'] or not info['active']):
                    redeemable.append(info)

            seen = set()
            unique = []
            for r in redeemable:
                if r['condition_id'] not in seen:
                    seen.add(r['condition_id'])
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
            acct_addr = self.w3.to_checksum_address(os.getenv("FUNDER_ADDRESS"))
            
            func = self.ctf_contract.functions.redeemPositions(
                self.w3.to_checksum_address(market_info['collateral']),
                bytes(32), 
                bytes.fromhex(market_info['condition_id'][2:]),
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

# --- Risk Engine ---
class RiskEngine:
    def __init__(self, client: ClobClient, config: ConfigManager):
        self.client = client
        self.config = config
        self.positions = {}
        self.usdc_balance = Decimal(0)
        self.last_sync = 0

    async def sync_data(self):
        loop = asyncio.get_running_loop()
        try:
            logger.info("📥 Syncing Portfolio Data...")
            bal = await loop.run_in_executor(None, self.client.get_collateral_balance)
            self.usdc_balance = Decimal(bal) if bal else Decimal(0)
            
            pos_resp = await loop.run_in_executor(None, lambda: self.client.get_positions(limit=100))
            self.positions = {p['asset_id']: Decimal(p['size']) for p in pos_resp}
            self.last_sync = time.time()
            logger.info(f"💳 Balance: ${self.usdc_balance:.2f} | Positions: {len(self.positions)}")
        except Exception as e:
            logger.error(f"❌ Risk Sync Error: {e}")

    async def get_execution_price(self, token_id: str, side: str) -> Optional[Decimal]:
        loop = asyncio.get_running_loop()
        book = await loop.run_in_executor(None, lambda: self.client.get_order_book(token_id))

        if not book.asks or not book.bids: 
            logger.warning(f"⚠️ SKIP {token_id}: Empty Orderbook")
            return None

        best_ask = Decimal(book.asks[0].price)
        best_bid = Decimal(book.bids[0].price)
        return best_ask if side == "BUY" else best_bid

    def check_cumulative_limit(self, trader_addr, token_id, current_price, new_cost):
        max_pos = Decimal(self.config.get(trader_addr, "max_position_usdc", 50))
        
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
            logger.info(f"⚙️ Processing Signal: {signal.side} for {signal.question[:20]}...")

            mkt_price = await self.risk.get_execution_price(signal.token_id, signal.side)
            if not mkt_price: return

            addr = signal.trader_address
            mode = self.config.get(addr, "sizing_mode", "fixed")
            
            target_shares = Decimal(0)
            if mode == "proportional":
                fraction = Decimal(self.config.get(addr, "mirror_fraction", 0.1))
                target_shares = signal.size_shares * fraction
            else:
                fixed_usd = Decimal(self.config.get(addr, "fixed_amount", 10))
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

            slippage = Decimal(self.config.get(addr, "max_slippage", 0.035))
            
            if signal.side == "BUY":
                limit_price = mkt_price * (1 + slippage)
                limit_price = min(limit_price, Decimal("0.99"))
            else:
                limit_price = mkt_price * (1 - slippage)
                limit_price = max(limit_price, Decimal("0.02"))

            logger.info(f"🚀 SENDING ORDER: {signal.side} {target_shares} @ {limit_price:.3f} (Mkt: {mkt_price:.3f})")
            
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
                filled = Decimal(resp.size_matched) if hasattr(resp, 'size_matched') else target_shares
                if filled < target_shares:
                    logger.warning(f"⚠️ PARTIAL FILL: {filled}/{target_shares} shares")
                else:
                    logger.info(f"✅ FULL FILL: {filled} shares executed.")
            else:
                err = getattr(resp, 'errorMsg', 'Unknown')
                logger.error(f"❌ ORDER FAILED: {err}")

        except Exception as e:
            logger.error(f"❌ Execution Exception: {e}")

    async def run(self):
        self.running = True
        while self.running:
            signal = await self.queue.get()
            await self.execute(signal)
            self.queue.task_done()

# --- WSS Monitor ---
class WSSMonitor:
    def __init__(self, config: ConfigManager, engine: ExecutionEngine, asset_mgr: AssetManager):
        self.config = config
        self.engine = engine
        self.asset_mgr = asset_mgr
        self.running = False

    async def process_log(self, log, contract):
        try:
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

            args = event_data['args']
            src = args['from'].lower()
            dst = args['to'].lower()

            trader, side = None, None
            if src in self.config.addresses:
                trader, side = src, "SELL"
            elif dst in self.config.addresses:
                trader, side = dst, "BUY"
            
            if not trader: return

            ids = args['ids'] if is_batch else [args['id']]
            values = args['values'] if is_batch else [args['value']]

            for tid, raw_val in zip(ids, values):
                info = await self.asset_mgr.get_market_info(tid)
                if not info:
                    logger.warning(f"⚠️ SKIP: Market info failed for ID {tid}")
                    continue
                
                if not info['active']:
                    logger.info(f"ℹ️ SKIP: Market inactive ({info['question']})")
                    continue
                
                decimals = await self.asset_mgr.get_decimals(info['collateral'])
                if decimals is None: 
                    logger.error(f"❌ SKIP: Unknown decimals for {info['collateral']}")
                    continue

                size_shares = Decimal(raw_val) / Decimal(10**decimals)
                if size_shares < 0.5: 
                    logger.info(f"ℹ️ SKIP: Dust trade ({size_shares:.2f} shares)")
                    continue

                signal = TradeSignal(
                    timestamp=datetime.now(),
                    token_id=str(tid),
                    condition_id=info['condition_id'],
                    side=side,
                    size_shares=size_shares,
                    trader_address=trader,
                    tx_hash=log['transactionHash'].hex(),
                    question=info['question'],
                    collateral_token=info['collateral']
                )
                
                logger.info(f"🔔 SIGNAL DETECTED: {trader[:6]} {side} {size_shares:.1f} | {info['question'][:30]}...")
                await self.engine.queue.put(signal)

        except Exception as e:
            logger.error(f"❌ Log Processing Error: {e}")

    async def start(self):
        self.running = True
        while self.running:
            try:
                logger.info(f"🔌 Connecting WSS: {POLYGON_WSS}...")
                # 🚨 V7 FIX: WebSocketProvider (renamed from V2)
                async with AsyncWeb3(WebSocketProvider(POLYGON_WSS)) as w3:
                    if not await w3.is_connected():
                        raise ConnectionError("WSS Handshake Failed")
                    
                    contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
                    await w3.eth.subscribe("logs", {"address": CTF_ADDRESS})
                    logger.info("✅ WSS Connected & Subscribed")
                    
                    # 🚨 V7 FIX: Correct Payload Parsing for Subscription
                    async for payload in w3.socket.process_subscriptions():
                        result = payload.get("result")
                        if not result and "params" in payload:
                            result = payload["params"].get("result")
                        
                        if result:
                            await self.process_log(result, contract)
            
            except Exception as e:
                logger.error(f"⚠️ WSS Disconnected: {e}. Retry in 5s...")
                await asyncio.sleep(5)

# --- Main ---
async def main():
    logger.info("=== 🦅 POLYMARKET V8.1 STARTUP ===")
    config = ConfigManager()
    
    # Init Web3
    w3_http = AsyncWeb3(AsyncHTTPProvider(POLYGON_HTTP))
    if not await w3_http.is_connected():
        logger.critical("❌ HTTP RPC Connection Failed")
        return
        
    asset_mgr = AssetManager(w3_http)
    
    # Init Client
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
    clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
    del pk
    gc.collect()

    # Init Components
    risk = RiskEngine(clob_client, config)
    engine = ExecutionEngine(config, risk)
    monitor = WSSMonitor(config, engine, asset_mgr)
    redeemer = AutoRedeemer(w3_http, clob_client, asset_mgr)
    
    await asyncio.gather(
        monitor.start(),
        engine.run(),
        redeemer.start()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown.")