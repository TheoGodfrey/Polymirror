#!/usr/bin/env python3
"""
Polymarket Mirror Bot (V8.3 - Large ID & Position Fix)
--------------------------------------------
✅ FIX: Large CTF Token ID handling (36488135...)
✅ FIX: get_positions() method call (Removed limit arg)
✅ FIX: Switched to CLOB API for Market Metadata resolution
"""

import sys
import io
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

# Force UTF-8 for Windows
if sys.platform.startswith('win'):
    import os
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', write_through=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', write_through=True)

try:
    from web3 import AsyncWeb3, AsyncHTTPProvider, WebSocketProvider
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:
    print("CRITICAL: pip install web3>=7.0.0 py-clob-client python-dotenv requests")
    exit(1)

load_dotenv()

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler("bot_v8.log", encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger("PolyV8.3")

# --- Constants ---
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CLOB_API = "https://clob.polymarket.com"
CHAIN_ID = 137

# --- ABIs ---
CTF_ABI = [
    {"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"operator","type":"address"},{"indexed":True,"internalType":"address","name":"from","type":"address"},{"indexed":True,"internalType":"address","name":"to","type":"address"},{"indexed":False,"internalType":"uint256","name":"id","type":"uint256"},{"indexed":False,"internalType":"uint256","name":"value","type":"uint256"}],"name":"TransferSingle","type":"event"},
    {"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"operator","type":"address"},{"indexed":True,"internalType":"address","name":"from","type":"address"},{"indexed":True,"internalType":"address","name":"to","type":"address"},{"indexed":False,"internalType":"uint256[]","name":"ids","type":"uint256[]"},{"indexed":False,"internalType":"uint256[]","name":"values","type":"uint256[]"}],"name":"TransferBatch","type":"event"},
    {"inputs":[{"internalType":"address","name":"collateralToken","type":"address"},{"internalType":"bytes32","name":"parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}
]
ERC20_ABI = [{"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"payable":False,"stateMutability":"view","type":"function"}]

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

class AssetManager:
    def __init__(self, w3: AsyncWeb3):
        self.w3 = w3
        self.cache = {}
        self.token_decimals = {"0x2791bca1f2de4661ed88a30c99a7a9449aa84174": 6}

    async def get_decimals(self, token_addr: str) -> Optional[int]:
        t_addr = token_addr.lower()
        if t_addr in self.token_decimals: return self.token_decimals[t_addr]
        try:
            c = self.w3.eth.contract(address=self.w3.to_checksum_address(t_addr), abi=ERC20_ABI)
            d = await c.functions.decimals().call()
            self.token_decimals[t_addr] = d
            return d
        except: return 6 # Fallback to USDC decimals

    async def get_market_info(self, token_id):
        tid = str(token_id)
        if tid in self.cache: return self.cache[tid]
        
        # ✅ FIX: For Large IDs, query CLOB markets directly
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: requests.get(f"{CLOB_API}/markets/{tid}", timeout=5))
            if resp.ok:
                data = resp.json()
                info = {
                    "question": data.get("description", "Unknown"),
                    "condition_id": data.get("condition_id"),
                    "collateral": data.get("collateral_token", "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"),
                    "active": data.get("active", True),
                    "closed": False
                }
                self.cache[tid] = info
                return info
        except Exception as e:
            logger.debug(f"CLOB detail fetch failed for {tid[:10]}: {e}")
        return None

class SecureSigner:
    def __init__(self, w3: AsyncWeb3):
        self.w3 = w3
    async def get_gas(self):
        try:
            b = await self.w3.eth.get_block("latest")
            p = await self.w3.eth.max_priority_fee
            return {'maxFeePerGas': (b['baseFeePerGas'] * 2) + p, 'maxPriorityFeePerGas': p}
        except: return {'maxFeePerGas': 400_000_000_000, 'maxPriorityFeePerGas': 35_000_000_000}
    async def sign_send(self, tx):
        pk = os.getenv("PRIVATE_KEY")
        try:
            s = self.w3.eth.account.sign_transaction(tx, pk)
            return await self.w3.eth.send_raw_transaction(s.raw_transaction)
        finally: del pk; gc.collect()

class AutoRedeemer:
    def __init__(self, w3, client, amgr):
        self.w3, self.client, self.amgr = w3, client, amgr
        self.signer = SecureSigner(w3)
        self.ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)

    async def run_cycle(self):
        try:
            logger.info("♻️ Checking positions...")
            loop = asyncio.get_running_loop()
            # ✅ FIX: No 'limit' argument
            def fetch():
                if hasattr(self.client, 'get_positions'): return self.client.get_positions()
                return self.client.data_client.get_positions()
            
            pos = await loop.run_in_executor(None, fetch)
            if not pos: return

            for p in pos:
                if Decimal(str(p.get('size', 0))) <= 0: continue
                info = await self.amgr.get_market_info(p.get('asset_id'))
                if info and info.get('closed'):
                    await self.redeem(info)
        except Exception as e: logger.error(f"Redeemer Error: {e}")

    async def redeem(self, m):
        try:
            logger.info(f"💰 Redeeming {m['question'][:30]}...")
            addr = self.w3.to_checksum_address(os.getenv("FUNDER_ADDRESS"))
            tx = await self.ctf.functions.redeemPositions(
                self.w3.to_checksum_address(m['collateral']), bytes(32),
                bytes.fromhex(m['condition_id'][2:]), [1, 2]
            ).build_transaction({
                'from': addr, 'nonce': await self.w3.eth.get_transaction_count(addr),
                'chainId': CHAIN_ID, **await self.signer.get_gas()
            })
            h = await self.signer.sign_send(tx)
            logger.info(f"✅ Redeemed: {h.hex()}")
        except Exception as e: logger.error(f"Redeem Fail: {e}")

class ExecutionEngine:
    def __init__(self, client, amgr):
        self.client, self.amgr = client, amgr
        self.queue = asyncio.Queue()

    async def run(self):
        while True:
            sig = await self.queue.get()
            try:
                # Execution logic for large IDs
                price_resp = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.client.get_order_book(sig.token_id)
                )
                best_price = price_resp.asks[0].price if sig.side == "BUY" else price_resp.bids[0].price
                
                # Setup order args with large token_id string
                args = OrderArgs(
                    token_id=sig.token_id,
                    price=float(best_price),
                    size=10.0, # Fixed example size
                    side=BUY if sig.side == "BUY" else SELL,
                    order_type=OrderType.FAK
                )
                res = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.client.create_and_post_order(args)
                )
                logger.info(f"🚀 Execution Result: {getattr(res, 'success', False)}")
            except Exception as e: logger.error(f"Exec Error: {e}")
            finally: self.queue.task_done()

class WSSMonitor:
    def __init__(self, amgr, queue):
        self.amgr, self.queue = amgr, queue
        self.traders = {os.getenv("FUNDER_ADDRESS").lower()} # Monitor self + others in whitelist

    async def start(self):
        while True:
            try:
                async with AsyncWeb3(WebSocketProvider(os.getenv("POLYGON_WS"))) as w3:
                    contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
                    await w3.eth.subscribe("logs", {"address": CTF_ADDRESS})
                    async for p in w3.socket.process_subscriptions():
                        res = p.get("result") or p.get("params", {}).get("result")
                        if res: await self.handle_log(res, contract)
            except Exception as e:
                logger.warning(f"WSS Error: {e}. Restarting...")
                await asyncio.sleep(5)

    async def handle_log(self, log, contract):
        try:
            ev = contract.events.TransferSingle().process_log(log)
            args = ev['args']
            side = None
            if args['from'].lower() in self.traders: side = "SELL"
            elif args['to'].lower() in self.traders: side = "BUY"
            
            if side:
                tid = str(args['id']) # The large 36488... ID
                info = await self.amgr.get_market_info(tid)
                if info:
                    await self.queue.put(TradeSignal(
                        datetime.now(), tid, info['condition_id'], side,
                        Decimal(args['value'])/10**6, args['to'], log['transactionHash'].hex(),
                        info['question'], info['collateral']
                    ))
                    logger.info(f"🔔 Signal for Large ID: {tid[:10]}...")
        except: pass

async def main():
    w3 = AsyncWeb3(AsyncHTTPProvider(os.getenv("POLYGON_HTTP")))
    amgr = AssetManager(w3)
    client = ClobClient(CLOB_API, key=os.getenv("PRIVATE_KEY"), chain_id=CHAIN_ID, funder=os.getenv("FUNDER_ADDRESS"))
    client.set_api_creds(client.create_or_derive_api_creds())
    
    engine = ExecutionEngine(client, amgr)
    monitor = WSSMonitor(amgr, engine.queue)
    redeemer = AutoRedeemer(w3, client, amgr)

    await asyncio.gather(monitor.start(), engine.run(), redeemer.run_cycle())

if __name__ == "__main__":
    asyncio.run(main())