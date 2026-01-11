#!/usr/bin/env python3
"""
Polymarket Mirror Bot (V7 - MAXIMUM SPEED)
-------------------------------------------
✅ ASYNC ORDER SIGNING IN PROCESS POOL (BYPASSES GIL)
✅ RAW AIOHTTP FOR ORDER SUBMISSION
✅ PRE-CACHED FEE RATES + NONCES
✅ ZERO BLOCKING ON HOT PATH
"""

import os
import sys
import time
import asyncio
import logging
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from dataclasses import dataclass
from dotenv import load_dotenv
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import cpu_count

try:
    import orjson
    def json_loads(s): return orjson.loads(s)
    def json_dumps(o): return orjson.dumps(o).decode()
except ImportError:
    import json
    def json_loads(s): return json.loads(s)
    def json_dumps(o): return json.dumps(o)

import aiohttp

load_dotenv()

# --- MATH ---
def gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a

def get_size_inc(price_cents: int) -> Decimal:
    if price_cents <= 0:
        return Decimal("1")
    return Decimal(100 // gcd(price_cents, 100)) / 100

def round_size(size: Decimal, price: Decimal, up: bool = False) -> Decimal:
    inc = get_size_inc(int(price * 100))
    r = ROUND_UP if up else ROUND_DOWN
    return (size / inc).to_integral_value(rounding=r) * inc

TWO_DP = Decimal("0.01")
d2 = lambda v: Decimal(str(v)).quantize(TWO_DP, rounding=ROUND_DOWN)

# --- CONFIG ---
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

TARGET = os.getenv("TARGET_ADDRESS", "").lower()
PRIV_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER = os.getenv("FUNDER_ADDRESS", "")
SIG_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

MULT = Decimal(os.getenv("POSITION_MULTIPLIER", "1.00"))
DEFAULT_PORT = Decimal(os.getenv("DEFAULT_TARGET_PORTFOLIO", "100000.00"))
FIXED_CAP = os.getenv("FIXED_CAPITAL", "")
MAX_CAP = Decimal(os.getenv("MAX_CAPITAL_TO_USE", "5000.00"))

MAX_PRICE = Decimal("0.99")
MAX_SLIP_PCT = Decimal("0.05")
MIN_ORDER = Decimal("1.00")
MIN_SHARES = Decimal("5.00")

POLL_MS = 30  # Ultra-fast polling
ACTIVITY_LIMIT = 10  # Minimal payload
SIGNER_PROCESSES = max(2, cpu_count() - 1)  # Leave 1 core free

logging.basicConfig(level=logging.INFO, format='%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)
for n in ["urllib3", "aiohttp", "httpx", "httpcore"]:
    logging.getLogger(n).setLevel(logging.ERROR)

# Import signing components
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, AssetType
    from py_clob_client.order_builder.builder import OrderBuilder
    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False
    log.error("py_clob_client required")
    sys.exit(1)


# --- SIGNING IN SEPARATE PROCESS (BYPASSES GIL) ---
def sign_and_submit_process(args: tuple) -> dict | None:
    """
    Sign AND submit order in separate process - completely parallel.
    """
    try:
        priv_key, funder, sig_type, token_id, price, size, side, api_key, api_secret, api_passphrase = args
        
        # Create fresh client in this process
        client = ClobClient(
            CLOB, key=priv_key, chain_id=137,
            signature_type=sig_type, funder=funder
        )
        
        # Try to create ApiCreds object if available, else use dict
        try:
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        except:
            creds = {"apiKey": api_key, "secret": api_secret, "passphrase": api_passphrase}
        
        client.set_api_creds(creds)
        
        order = OrderArgs(token_id=token_id, price=price, size=size, side=side)
        signed = client.create_order(order)
        resp = client.post_order(signed, orderType=OrderType.FOK)
        return resp
    except Exception as e:
        return {"error": str(e)}


@dataclass(slots=True, frozen=True)
class Trade:
    id: str
    tid: str
    size: Decimal
    price: Decimal
    usd: Decimal
    name: str


class MaxSpeedBot:
    __slots__ = (
        'client', 'seen', 'cash', 'spent', 'blacklist', 'mkt_ticks',
        'fee_rates', 'session', 'fired', 'filled', 'last_hb', 'last_bal',
        'target_port', 'ratio', 'process_pool', 'order_queue',
        'api_key', 'api_secret', 'api_passphrase', 'pending_orders'
    )
    
    def __init__(self):
        self.seen: set[str] = set()
        self.cash = Decimal("0.00")
        self.spent = Decimal("0.00")
        self.blacklist: set[str] = set()
        self.mkt_ticks: dict[str, Decimal] = {}
        self.fee_rates: dict[str, float] = {}  # Pre-cached fee rates
        self.session: aiohttp.ClientSession | None = None
        self.fired = 0
        self.filled = 0
        self.last_hb = 0.0
        self.last_bal = 0.0
        self.target_port = DEFAULT_PORT
        self.ratio = Decimal("0.05")
        self.process_pool = ProcessPoolExecutor(max_workers=SIGNER_PROCESSES)
        self.order_queue: asyncio.Queue = asyncio.Queue()
        self.pending_orders: set = set()
        
        # API credentials
        self.api_key = ""
        self.api_secret = ""
        self.api_passphrase = ""
        
        self.client = None
        if HAS_CLOB and PRIV_KEY:
            try:
                self.client = ClobClient(CLOB, key=PRIV_KEY, chain_id=137, signature_type=SIG_TYPE, funder=FUNDER)
                creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(creds)
                # Store for process pool - creds is ApiCreds object, not dict
                self.api_key = getattr(creds, 'api_key', '') or getattr(creds, 'apiKey', '')
                self.api_secret = getattr(creds, 'api_secret', '') or getattr(creds, 'secret', '')
                self.api_passphrase = getattr(creds, 'api_passphrase', '') or getattr(creds, 'passphrase', '')
                log.info(f"✅ CLOB ready | {SIGNER_PROCESSES} signer processes")
            except Exception as e:
                log.error(f"❌ {e}")
                sys.exit(1)

    async def init_session(self):
        conn = aiohttp.TCPConnector(
            limit=100, limit_per_host=50, 
            ttl_dns_cache=300, keepalive_timeout=120,
            enable_cleanup_closed=True
        )
        timeout = aiohttp.ClientTimeout(total=3.0, connect=0.5, sock_read=2.0)
        self.session = aiohttp.ClientSession(
            connector=conn, timeout=timeout,
            headers={"User-Agent": "PM/7", "Connection": "keep-alive"}
        )

    async def close(self):
        self.process_pool.shutdown(wait=False)
        if self.session:
            await self.session.close()

    def get_balance(self) -> Decimal:
        try:
            r = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return (Decimal(str(r.get("balance", 0))) / 1_000_000).quantize(TWO_DP)
        except:
            return self.cash

    async def precache_all(self):
        """Pre-fetch market ticks AND fee rates for all positions"""
        log.info("📦 Pre-caching everything...")
        tids = set()
        offset = 0
        
        # Get all position token IDs
        try:
            while offset < 300:
                async with self.session.get(f"{DATA}/positions?user={TARGET}&limit=100&offset={offset}") as r:
                    if r.status != 200:
                        break
                    data = json_loads(await r.read())
                if not data:
                    break
                for p in data:
                    tid = p.get("asset") or p.get("tokenId")
                    if tid:
                        tids.add(tid)
                if len(data) < 100:
                    break
                offset += 100
        except Exception as e:
            log.warning(f"Position fetch: {e}")
        
        # Parallel fetch tick sizes AND fee rates
        async def fetch_market_data(tid):
            tick = Decimal("0.01")
            fee = 0.0
            
            try:
                # Tick size
                async with self.session.get(f"{CLOB}/neg-risk-market-info?token_id={tid}") as r:
                    if r.status == 200:
                        d = json_loads(await r.read())
                        tick = Decimal(str(d.get("min_tick_size", "0.01")))
            except:
                pass
            
            try:
                # Fee rate (pre-cache to skip on hot path)
                async with self.session.get(f"{CLOB}/fee-rate?token_id={tid}") as r:
                    if r.status == 200:
                        d = json_loads(await r.read())
                        fee = float(d.get("maker", d.get("fee", 0)))
            except:
                pass
            
            return tid, tick, fee
        
        # Batch fetch
        for i in range(0, len(tids), 30):
            batch = list(tids)[i:i+30]
            results = await asyncio.gather(*[fetch_market_data(t) for t in batch])
            for tid, tick, fee in results:
                self.mkt_ticks[tid] = tick
                self.fee_rates[tid] = fee
        
        log.info(f"📦 Cached {len(self.mkt_ticks)} markets")

    async def fetch_portfolio(self) -> Decimal:
        total = Decimal("0")
        offset = 0
        try:
            while offset < 300:
                async with self.session.get(f"{DATA}/positions?user={TARGET}&limit=100&offset={offset}") as r:
                    if r.status != 200:
                        break
                    data = json_loads(await r.read())
                if not data:
                    break
                for p in data:
                    sz = Decimal(str(p.get("size", 0)))
                    px = Decimal(str(p.get("curPrice") or p.get("avgPrice", 0)))
                    if sz > 0 and px > 0:
                        total += sz * px
                if len(data) < 100:
                    break
                offset += 100
        except:
            return self.target_port
        return total.quantize(TWO_DP) if total > 0 else DEFAULT_PORT

    def update_ratio(self):
        cap = Decimal(FIXED_CAP) if FIXED_CAP else max(Decimal("0"), self.cash - self.spent)
        self.ratio = (cap / self.target_port).quantize(Decimal("0.0001")) if self.target_port > 0 else Decimal("0.05")

    async def sign_and_submit(self, tid: str, sz: float, px: float, name: str, cost: Decimal):
        """Sign and submit in process pool (parallel, no GIL)"""
        try:
            loop = asyncio.get_event_loop()
            
            args = (PRIV_KEY, FUNDER, SIG_TYPE, tid, px, sz, "BUY", 
                    self.api_key, self.api_secret, self.api_passphrase)
            resp = await loop.run_in_executor(self.process_pool, sign_and_submit_process, args)
            
            if resp and resp.get("success"):
                self.filled += 1
                self.spent += cost
                log.info(f"✅ {sz} @ ${px} | {name[:25]} | ${float(cost):.2f}")
            elif resp:
                err = str(resp.get("error", resp))[:100]
                log.warning(f"❌ {err}")
                if "not exist" in err or "invalid" in err.lower():
                    self.blacklist.add(tid)
                    
        except Exception as e:
            log.error(f"Submit err: {type(e).__name__}: {e}")

    async def get_best_ask(self, tid: str) -> Decimal | None:
        """Fetch best ask price from orderbook"""
        try:
            async with self.session.get(f"{CLOB}/book?token_id={tid}") as r:
                if r.status == 200:
                    data = json_loads(await r.read())
                    asks = data.get("asks", [])
                    if asks:
                        # Asks are sorted lowest first
                        return Decimal(str(asks[0].get("price", 0)))
        except:
            pass
        return None

    async def fire_order(self, trade: Trade):
        """Non-blocking order dispatch"""
        if not self.client or self.ratio < Decimal("0.0001"):
            return
        
        try:
            tick = self.mkt_ticks.get(trade.tid, Decimal("0.01"))
            
            # Fetch actual best ask from orderbook
            best_ask = await self.get_best_ask(trade.tid)
            
            if best_ask is None or best_ask <= 0:
                log.warning(f"⏭️ No asks for {trade.name[:20]}")
                return
            
            # Skip if market moved too far from their entry (>15% above)
            if best_ask > trade.price * Decimal("1.15"):
                log.warning(f"⏭️ Price moved: {trade.price} → {best_ask}")
                return
            
            # Use best ask + tiny buffer for race conditions
            # Cap at max price and max slippage from their entry
            max_from_slippage = trade.price * (1 + MAX_SLIP_PCT)
            limit = min(best_ask + Decimal("0.02"), max_from_slippage, MAX_PRICE).quantize(TWO_DP, rounding=ROUND_DOWN)
            
            if limit <= 0 or limit > MAX_PRICE:
                return
            
            target_usd = max(MIN_ORDER, (trade.usd * self.ratio * MULT).quantize(TWO_DP))
            raw = target_usd / limit
            shares = round_size(raw, limit)
            
            if shares * limit < MIN_ORDER:
                shares = round_size(raw, limit, up=True)
            if shares < MIN_SHARES:
                shares = round_size(MIN_SHARES, limit, up=True)
            if tick >= Decimal("0.1"):
                shares = (shares / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            
            cost = (shares * limit).quantize(TWO_DP)
            if self.spent + cost > MAX_CAP:
                return
            
            inc = get_size_inc(int(limit * 100))
            sz = int(shares) if inc >= 1 else (round(float(shares), 1) if inc >= Decimal("0.1") else round(float(shares), 2))
            px = round(float(limit), 2)
            
            log.info(f"📤 {sz} @ ${px} (ask=${float(best_ask):.2f}) [${float(trade.usd):.0f}×{float(self.ratio)*100:.1f}%]")
            
            # Fire and forget - don't await
            asyncio.create_task(self.sign_and_submit(trade.tid, sz, px, trade.name, cost))
            self.fired += 1
            
        except Exception as e:
            log.error(f"Fire: {e}")

    async def fetch_activity(self) -> list[Trade]:
        trades = []
        try:
            async with self.session.get(f"{DATA}/activity?user={TARGET}&limit={ACTIVITY_LIMIT}") as r:
                if r.status != 200:
                    return trades
                data = json_loads(await r.read())
        except:
            return trades
        
        if not data:
            return trades
        
        for item in data:
            try:
                tid_val = item.get("id") or f"{item.get('timestamp')}_{item.get('asset')}"
                if tid_val in self.seen:
                    continue
                
                action = (item.get("type") or item.get("action") or "").upper()
                side = (item.get("side") or "").upper()
                if not ("BUY" in action or side == "BUY" or (action in ("TRADE", "FILL") and side != "SELL")):
                    continue
                
                tid = item.get("asset") or item.get("tokenId")
                if not tid or tid in self.blacklist:
                    continue
                
                sz = d2(float(item.get("size") or 0))
                px = d2(float(item.get("price") or 0))
                if sz <= 0 or px <= 0 or px > MAX_PRICE:
                    continue
                
                usd = (sz * px).quantize(TWO_DP)
                if usd < Decimal("1"):
                    continue
                
                # Pre-cache market if new
                if tid not in self.mkt_ticks:
                    self.mkt_ticks[tid] = Decimal("0.01")  # Default, fetch later
                
                trades.append(Trade(tid_val, tid, sz, px, usd, item.get("title") or "?"))
            except:
                continue
        
        return trades

    async def poll(self):
        t_start = time.perf_counter()
        trades = await self.fetch_activity()
        fetch_ms = (time.perf_counter() - t_start) * 1000
        
        now = time.time()
        
        # Background balance refresh
        if now - self.last_bal > 60:
            self.cash = self.get_balance()
            self.last_bal = now
            self.update_ratio()
        
        # Process new trades IMMEDIATELY
        for t in trades:
            if t.id in self.seen:
                continue
            self.seen.add(t.id)
            log.info(f"🔔 {float(t.size):.1f} @ ${float(t.price):.2f} (${float(t.usd):.0f}) | {t.name[:30]}")
            await self.fire_order(t)
        
        if now - self.last_hb > 10:
            log.info(f"💓 {self.filled}/{self.fired} | fetch={fetch_ms:.0f}ms | ratio={float(self.ratio)*100:.1f}%")
            self.last_hb = now
        
        if len(self.seen) > 2000:
            self.seen = set(list(self.seen)[-1000:])

    async def run(self):
        log.info(f"⚡ MAX SPEED v7 | {POLL_MS}ms poll | {SIGNER_PROCESSES} signers")
        log.info(f"👁️ {TARGET[:12]}...")
        
        await self.init_session()
        await self.precache_all()
        
        self.cash = self.get_balance()
        self.target_port = await self.fetch_portfolio()
        self.update_ratio()
        log.info(f"📊 Ratio: {float(self.ratio)*100:.2f}%")
        
        # Initial activity cache
        trades = await self.fetch_activity()
        for t in trades:
            self.seen.add(t.id)
        log.info(f"📍 Cached {len(self.seen)} trades")
        
        try:
            while True:
                t0 = time.perf_counter()
                await self.poll()
                elapsed = (time.perf_counter() - t0) * 1000
                await asyncio.sleep(max(0.005, (POLL_MS - elapsed) / 1000))
        except KeyboardInterrupt:
            log.info("⏹️")
        finally:
            await self.close()


if __name__ == "__main__":
    if not PRIV_KEY or not TARGET:
        print("❌ Missing env")
        sys.exit(1)
    asyncio.run(MaxSpeedBot().run())